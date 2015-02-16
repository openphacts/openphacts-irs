#!/usr/bin/env python3
import sys
import time
import json
import uuid
import collections
from urllib.request import FancyURLopener
from urllib.parse import urljoin, quote, urlencode, unquote, urlparse

import yaml

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from elasticsearch.exceptions import NotFoundError

try:
    import ijson.backends.yajl2 as ijson
except ex:
    print("Can't find py-yajl, JSON parsing will be slower")
    import ijson

REPORT_EVERY=1200
DEFAULT_SPARQL_TIMEOUT=60

# SPARQL variable name for the resource
ID="id"
# and for the rdf:type (on subclasses)
TYPE="type"

class SlidingWindowDictionary(collections.OrderedDict):
    def __init__(self, max_size=1000):
        self.max_size = max_size
        super().__init__()
    def __setitem__(self, key, value):
        while (len(self) >= self.max_size):
            self.popitem(last=False)
        super().__setitem__(key, value)

def negate(f):
    return lambda *args,**kwargs: not f(*args, **kwargs) 

def is_property_required(prop):
    return bool(prop.get("required", False))

class Session:
    def __init__(self, config_file):
        with open(config_file) as f:
            self.conf = yaml.load(f)

        es_hosts = self.conf.get("elasticsearch")
        print("*****")
        print("SPARQL endpoint: " + self.conf["sparql"]["uri"])
        print("ElasticSearch: %s" % es_hosts)
        print("*****")
        self.es = Elasticsearch(es_hosts)

        self.urlOpener = FancyURLopener()
        self.urlOpener.addheader("Accept",
            "application/sparql-results+json, applicaton/json;q=0.1")

    def uri_to_qname(self,uri):
        for p,u in self.conf.get("prefixes", {}).items():
            if uri.startswith(u):
                return uri.replace(u, p+":", 1)
        return uri

    def sparql_prefixes(self):
        sparql=[]
        for p,u in self.conf.get("prefixes", {}).items():
            sparql.append("PREFIX %s: <%s>" % (p,u))
        return "\n".join(sparql)

    def run(self):
        for index in self.conf["indexes"]:
            try:
                self.es.indices.delete(index=index)
            except NotFoundError:
                pass
            for doc_type in self.conf["indexes"][index]:
                indexer = Indexer(self, index, doc_type)
                ## TODO: Store mapping for JSON-LD
                indexer.load()

    def check(self):
        self.check_prefixes()
        self.check_required_properties()

    def expand_qname(self, p):
        if not ":" in p:
            raise Exception("Invalid property, no prefix: " + p)
        prefix,rest = p.split(":", 1)
        if not prefix in self.conf.get("prefixes", {}):
            raise Exception("Unknown prefix: " + prefix)
        base = self.conf.get("prefixes")[prefix]
        return base + rest

    def check_property(self, p):
        if type(p) == str:
            urlparse(self.expand_qname(p))
        else:
            ## Assume it is dict-based - check they are all non-empty
            if not p.get("sparql"):
                raise Exception("'sparql' missing for %s" % p)
            if not p.get("variable"):
                raise Exception("'variable' missing for %s" % p)
            if not p.get("jsonld"):
                raise Exception("'jsonld' missing for %s" % p)

    def check_required_properties(self):
        # Check that every index+type have at least one
        # required triple (rdf:type or a property)

        for p in self.conf["common_properties"]:
            if type(p) != str and is_property_required(p):
                return # Great! required for every index

        # if not, we'll need to check each index+type
        for index,index_conf in self.conf["indexes"].items():
            for doc_type,type_conf in index_conf.items():
                if "type" in type_conf:
                    continue # OK
                for p in type_conf.get("properties", []):
                    if is_property_required(p):
                        continue # OK
                raise Exception("No type: or property with required:true for %s %s" % (index, doc_type))

    def check_prefixes(self):
        for uri in self.conf.get("prefixes", {}).values():
            if not (uri.endswith("#") or uri.endswith("/")):
                # This should catch prefix definitions not ending with / #
                print("WARNING: Prefix doesn't end with / or #: %s" % uri,
                    file=sys.stderr)
        for p in self.conf.get("common_properties", []):
            self.check_property(p)
        for index,index_conf in self.conf["indexes"].items():
            for doc_type,type_conf in index_conf.items():
                if "type" in type_conf:
                    urlparse(self.expand_qname(type_conf["type"]))
                for p in type_conf.get("properties", []):
                    self.check_property(p)
                    



class Indexer:
    def __init__(self, session, index, doc_type):
        self.cache = SlidingWindowDictionary()
        self.session = session
        self.index = index
        self.doc_type = doc_type
        self.conf = self.session.conf["indexes"][index][doc_type]
        self.indexed = set()
        self.reset_stats()
        self.properties = {}
        self.blanknodes = {}

    def sort_properties(self, properties):
        properties.sort(key=negate(is_property_required))
        return properties

    def reset_stats(self):
        self.count = 0
        self.start = time.time()
        self.lastCheck = self.start

    def variable_for_property_name(self, prop):
        short = prop.split(":")[-1]
        if short not in self.properties:
            return short
        u = str(uuid.uuid5(uuid.NAMESPACE_URL, self.session.expand_qname(prop))).replace("-", "")
        print("WARNING: non-unique short-name for %s, falling back to %s" % (prop, u),
            file=sys.stderr)
        return u


    def sparql_property(self, prop):
        sparql = "    ?%s %s ?%s ." % (ID, prop["sparql"], prop["variable"])
        if not is_property_required(prop):
            return self.sparql_optional(sparql)
        return sparql

    def sparql_optional(self, sparql):
        return "    OPTIONAL { %s }" % sparql.strip()

    def expand_property(self, p):
        if type(p) == str:
            variable = self.variable_for_property_name(p)
            p = {
                    "sparql": p,
                    "variable": variable,
                    "jsonld": variable
                }
        name = p["variable"]
        if name in self.properties:
            raise Exception("Duplicate property name " + name)
        self.properties[name] = p
        return p

    def sparql(self):
        sparql = []
        sparql.append(self.session.sparql_prefixes())
        sparql.append("SELECT *")
        sparql.append("WHERE {")

        if "graph" in self.conf:
            sparql.append(" GRAPH <%s> {" % self.conf["graph"])

        if "type" in self.conf:
            rdf_type = self.conf["type"]
            sparql.append("   { ?%s a %s . }" % (ID, rdf_type))

            subclasses = self.conf.get("subclasses")
            if subclasses:
                sparql.append("   UNION {")
                sparql.append("     ?%s a ?subClass . " % ID)
                if subclasses == "owl":
                    sparql.append("     ?subClass a owl:Class . ")
                    sparql.append("     ?subClass rdfs:subClassOf+ %s ." % rdf_type)
                else:
                    sparql.append("     ?subClass rdfs:subClassOf %s ." % rdf_type)
                sparql.append("   }")

        properties = []
        properties.extend(map(self.expand_property, self.session.conf.get("common_properties", [])))
        properties.extend(map(self.expand_property, self.conf.get("properties", [])))

        if not properties:
            raise Exception("No properties configured for %s %s" % (self.index, self.doc_type))
        self.sort_properties(properties)

        #print("Properties:\n  ", " ".join(properties))
        props_sparql = map(self.sparql_property, properties)
        sparql.extend(props_sparql)


        if "graph" in self.conf:
            sparql.append(" }")
        sparql.append("}")
        if "limit" in self.session.conf["sparql"]:
            sparql.append("LIMIT %s" % self.session.conf["sparql"]["limit"])

        sparqlStr = "\n".join(sparql)
        print("SPARQL query:")
        print()
        print(sparqlStr)
        print()
        return sparqlStr

    def sparqlURL(self):
        timeout = int(self.session.conf["sparql"].
                        get("timeout_s",
                            DEFAULT_SPARQL_TIMEOUT)) * 1000
        return urljoin(self.session.conf["sparql"]["uri"],
            "?" + urlencode(dict(query=self.sparql(),
                                 timeout=timeout)))

    def json_reader(self):
        url = self.sparqlURL()
        with self.session.urlOpener.open(url) as jsonFile:
            bindings = ijson.items(jsonFile, "results.bindings.item")
            for binding in bindings:
                #print(binding)
                n = self.binding_as_doc(binding)
                if n is not None:
                    #print(n)
                    yield n

    def skolemize(self, bnode):
        if bnode not in self.blanknodes:
            self.blanknodes[bnode] = uuid.uuid4()
        return self.blanknodes[bnode].urn

    def unescape(self, result):
        value = result["value"]
        if result["type"] == "uri":
            ## Poor man's IRI parsing
            ## Should really parse as rfc3987
            return unquote(value)
        return value

    def update_script_for(self, body):
        script=[]
        for var in body:
            if var.startswith("@"):
                continue

            prop = self.properties[var]
            jsonld = prop["jsonld"]

            script.append("if (! ctx._source.containsKey('%s')) {" % jsonld)
            script.append("  ctx._source['%s'] = [%s] " % (jsonld,var))
            script.append("} else ")
            script.append("if (! ctx._source['%s'].contains(%s)) {" % (jsonld,var))
            script.append("  ctx._source['%s'] += %s" % (jsonld,var))
            script.append("}")

        return "\n".join(script)

    def merge_bodies(self, old, new):
        body = {}
        keys = set(old.keys())
        keys.update(new.keys())
        for k in keys:
            if k == "@id":
                # we don't want a list for @id,
                # and they should match
                body[k] = old[k]
                continue
            # We'll modify old_v in-place (create if it does not exists)
            old_v = old.get(k, [])
            new_v = new.get(k, set())
            old_v.extend(list(set(new_v) - set(old_v))) # avoid duplicates
            body[k] = old_v
        return body

    def print_statistics(self):
        now = time.time()
        speed = REPORT_EVERY/(now-self.lastCheck)
        avgSpeed = self.count/(now-self.start)
        print("n=%s, speed=%0.1f/sec, avgSpeed=%0.1f/sec" % (self.count, speed, avgSpeed))
        self.lastCheck = now

    def binding_as_doc(self, node):
        self.count += 1
        # Time for some statistics!
        if (self.count % REPORT_EVERY == 0):
            self.print_statistics()

        if node[ID]["type"] == "uri":
            uri = self.unescape(node[ID])
        else:
            # id is a blank node, we'll need to make a new id
            uri = self.skolemize(node[ID]["value"])

        body = { "@id": uri }
        params = {}
        types = []
        if "type" in self.conf:
            types.append(self.conf["type"])
        if TYPE in node:
            types.append(node[TYPE]["value"])
            params["__type"] = node[TYPE]["value"]
        if types:
            body["@type"] = types


        for var in node:
            if var in (ID,TYPE):
                continue
            #print(var, node[var])
            if var == "subClass":
                prop = "subClassOf"
                jsonld = "rdfs:subClassOf"
            else:
                prop = self.properties[var]
                jsonld = prop["jsonld"]

            if node[var] is None or node[var].get("value") is None:
                params[var] = None
                body[jsonld] = []
            else:
                value = node[var]["value"]
                params[var] = value
                body[jsonld] = [ value ]

        doc_id = uri

        # Return ElasticSearch bulk message
        msg = { "_index": self.index,
                "_type": self.doc_type,
                "_id": doc_id
               }

        doc_uuid = uuid.uuid5(uuid.NAMESPACE_URL, uri)
        if not doc_uuid in self.indexed:
            self.indexed.add(doc_uuid)
            self.cache[doc_uuid] = body
            msg["_source"] = body
            #print("Cache save")
        else:
            # We'll need to update it. We are however part of a
            # bulk operation and can't request ElasticSearch for
            # the old document.

            # Let's hope we still have it in memory, and we
            # can simply merge and put it a second time
            cached = self.cache.get(doc_uuid)
            if cached:
                body = self.merge_bodies(cached, body)
                msg["_source"] = body
                self.cache[doc_uuid] = body
                #print("Cache hit")
            else:
                #print("Cache miss")
                ## NOTE: We can't put body in self.cache as it is only partial

                # We've forgotten about it.. Let's do the
                # update server-side (slower)
                msg.update({
                    "_op_type": "update",
                    "script": self.update_script_for(body),
                    "params": params,
                    "upsert": body
                })
            # TODO: Investigate alternative data structures (child documents?)
            # to avoid update logic
        return msg

    def load(self):
        print("Index %s type %s" % (self.index, self.doc_type))
        self.reset_stats()
        bulk(self.session.es, self.json_reader(), raise_on_error=True)
        # final statistics
        self.print_statistics()

def main(*args):
    if not args or args[0] in ("-h", "--help"):
        print("Usage: %s [config]" % sys.argv[0])
        print("")
        print("See example.yaml for an example configuraton file")
        print("and README.md for details.")
        return 0
    session = Session(args[0])
    session.check()
    session.run()



if __name__ == "__main__":
    exit = main(*sys.argv[1:])
    sys.exit(exit)
