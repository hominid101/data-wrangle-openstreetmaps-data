#!/usr/bin/env python
import os
import bz2
import copy
import csv
import re
#import xml.etree.ElementTree as ET
import xml.etree.cElementTree as ET
from collections import defaultdict
import pprint
import json
import codecs

# Utility functions to find and uncompress files, find MongoDB instances etc.
def find_file(data_dir, fname):
    fullPath = os.path.join(data_dir, fname)
    fileRoot, fileExt = os.path.splitext(fullPath)
    if fileExt == '.zip':
        if not os.path.exists(fileRoot):
            from zipfile import ZipFile
            with ZipFile(fullPath, 'r') as myzip:
                #myzip.extractall()
                myzip.extract(fileRoot)
        fname = fileRoot
    elif fileExt == '.bz2':
        if not os.path.exists(fileRoot):
            from bz2 import BZ2File
            with BZ2File(fullPath, 'r') as mybzip:
                with open(fileRoot, 'w') as ofile:
                    for line in mybzip:
                        ofile.write(line)
        fname = fileRoot
    else:
        fname = fullPath
    return fname

# Get a MongoDb instance
def get_mongodb(db_name):
    # For local use
    from pymongo import MongoClient
    client = MongoClient('localhost:27017')
    db = client[db_name]
    return db


import xml.etree.ElementTree as ET  # Use cElementTree or lxml if too slow

OSM_FILE = "osm_file.osm"  # Replace this with your osm file

############################################################################
# Create a smaller sample of the osm file
############################################################################

def sample_element(osm_file, tags=('node', 'way', 'relation')):
    """Yield element if it is the right type of tag

    Reference:
    http://stackoverflow.com/questions/3095434/inserting-newlines-in-xml-file-generated-via-xml-etree-elementtree-in-python
    """
    context = ET.iterparse(osm_file, events=('start', 'end'))
    _, root = next(context)
    for event, elem in context:
        if event == 'end' and elem.tag in tags:
            yield elem
            root.clear()

def sample_elements(infname, sample_fname):
    with open(sample_fname, 'wb') as output:
        output.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        output.write('<osm>\n  ')
        
        # Write every 10th top level element
        for i, element in enumerate(sample_element(infname)):
            if i % 10 == 0:
                output.write(ET.tostring(element, encoding='utf-8'))

        output.write('</osm>')

############################################################################
# Audit and clean openstreet data programmatically by SAX parsing the xml file
############################################################################

"""Audit tags: 
Assess the size of the map by determining the number of occurences of
each tag
"""
def count_tags(fname):
    tags = {}
    with open(fname, "r") as osm_file:
        for event, elem in ET.iterparse(osm_file):
            if elem.tag in tags:
                tags[elem.tag] += 1
            else:
                tags[elem.tag] = 1
    return tags

def audit_tags(fname):
    tags = count_tags(fname)
    print "\nAuditing tags"
    print "=========================================================="
    pprint.pprint(tags)

"""Audit keys:

Check the "k" value for each "<tag>" and see if they can be valid keys
in MongoDB, as well as see if there are any other potential problems.

We use 3 regular expressions to check for certain patterns in the
tags. We would like to change the data model and expand the
"addr:street" type of keys to a dictionary like this: {"address":
{"street": "Some value"}}

"""
def key_type(element, keys):
    lower = re.compile(r'^([a-z]|_)*$')
    lower_colon = re.compile(r'^([a-z]|_)*:([a-z]|_)*$')
    problemchars = re.compile(r'[=\+/&<>;\'"\?%#$@\,\. \t\r\n]')
    if element.tag == "tag":
        key = element.attrib['k']
        if problemchars.search(key):
            keys["problemchars"] += 1
        elif lower_colon.search(key):
            keys["lower_colon"] += 1
        elif lower.search(key):
            keys["lower"] += 1
        else: 
            keys["other"] += 1
    return keys

def audit_keys(fname):
    keys = {"lower": 0, "lower_colon": 0, "problemchars": 0, "other": 0}
    with open(fname, "r") as osm_file:
        for _, element in ET.iterparse(osm_file):
            keys = key_type(element, keys)
    print "\nAuditing keys"
    print "=========================================================="
    pprint.pprint(keys)

"""Audit users: 

Find the number of contributions made by each user
"""
def get_user(element):
    if 'user' in element.attrib:
        return element.attrib['user']
    else:
        return None

def audit_users(fname):
    users = {}
    with open(fname, "r") as osm_file:
        for _, element in ET.iterparse(osm_file):
            if element.tag == "node" or element.tag == "way" :
                user = get_user(element)
                #users.add(get_user(element))
                if user in users:
                    users[user] += 1
                else:
                    users[user] = 1

    print "\nAuditing users"
    print "=========================================================="
    print "Number of users = ", len(users)
    pprint.pprint(users)
    if fname.endswith("example.osm"):
        assert len(users) == 8

"""Audit and clean street types: 

Collect all street types than need clean up in a dictionary street_types. 

Fix the street names and make them consistent according to a
convention specified in a name mapping table.
"""
street_type_re = re.compile(r'\b\S+\.?$', re.IGNORECASE)

def is_street_name(elem):
    return (elem.tag == "tag") and (elem.attrib['k'] == "addr:street")

def audit_street_type(street_name, rare_street_types):
    expected = ["Avenue","Boulevard", "Connector", "Commons", "Court", 
                "Drive", "Parkway", "Place","Lane","Road", "Row",
                "Sarani", "Square", "Street", "Trail"]
    m = street_type_re.search(street_name)
    if m:
        street_type = m.group()
        if street_type not in expected:
            rare_street_types[street_type].add(street_name)
    else:
        rare_street_types['UNKNOWN'].add(street_name)

street_mapping = { 
    "street": "Street",
    "st": "Street",
    "raod": "Road",
    "road": "Road",
    "rd": "Road",
    "avenue": "Avenue",
    "ave": "Avenue",
    "boulevard": "Boulevard",
    "blvd": "Boulevard",
    "drive": "Drive",
    "dr": "Drive",
    "circle": "Circle",
    "cir": "Circle",
    "court": "Court",
    "ct": "Court",
    "pally": "Pally",
    "place": "Place",
    "pl": "Place",
    "potty": "Potty",
    "square": "Square",
    "sqr": "Square",
    "lane": "Lane",
    "ln": "Lane"
}
def fix_street_name(name, mapping):
    fixed_name = name

    # Use more standard names for street types
    m = street_type_re.search(name)
    if m:
        street_type = m.group().rstrip('.').lower()
        if street_type in mapping:
            fixed_name = name[:-len(street_type)] + mapping[street_type]

    # If steet name contains street number, move the info to house number
    housenum = None
    housenum_re = re.compile(r'^\s*\d+/?\d*[a-zA-Z]?,?[^a-zA-Z]*')
    m = housenum_re.search(fixed_name)
    if m:
        re_match = m.group()
        housenum = re_match.rstrip().rstrip(',').lstrip()
        fixed_name = fixed_name[len(re_match):]
    if name != fixed_name:
        print "Cleaning street name: ", name, " to ", fixed_name
    return housenum, fixed_name


"""Audit and clean city names: 
Collect all city names in a dictionary city_names.

Fix the city names and make them consistent according to a convention
specified in a name mapping table.

"""
def is_city_name(elem):
    return (elem.tag == "tag") and (elem.attrib['k'] == "addr:city")

def audit_city_name(city_name, city_names):
    city_names.add(city_name)

city_mapping = { 
    'kolkata': 'Kolkata',
    'saltlake': 'Salt Lake (Bidhannagar)',
    'salt lake': 'Salt Lake (Bidhannagar)',
    'dum dum cantt' : 'Dum Dum Cantonment, Kolkata',
    'bamangachi' : 'Bamangachi'
}

def fix_city_name(name, city_mapping):
    fixed_name = name
    first_word = name.lower().split(' ', 1)[0]
    if first_word in city_mapping:
        fixed_name = city_mapping[first_word]
    #if name != fixed_name:
    #    print "Cleaning city name: ", name, " to ", fixed_name
    return fixed_name

postcode_re = re.compile(r'\s*\d+\s*')
def is_postcode(elem):
    mykey = elem.attrib['k']
    return (mykey.startswith("addr:post") and mykey.endswith("code"))

def audit_postcode(tagelem, postcodes):
    isValid = False
    code = tagelem.attrib['v']
    pkey = tagelem.attrib['k']
    m = postcode_re.search(code)
    if m:
        re_match = m.group()
        pcode = re_match.rstrip().rstrip(',').lstrip()
        pcode_key = pkey+str(len(pcode))
        postcodes[pcode_key].add(pcode)
        if len(pcode) == 6:
            isValid = True
    else:
        postcodes[pkey+str(0)].add(code)
    return isValid

def fix_postcode(name):
    fixed_code = code
    if code != fixed_code:
        print "Cleaning post code: ", code, " to ", fixed_code
    return fixed_code
    


""" Audit and clean addresses:
Top level functions to clean and audit addresses.
"""
def is_housenum(elem):
    return (elem.tag == "tag") and (elem.attrib['k'] == "addr:housenum")

def clean_address(elem):
    housenum = None
    housenum_elem = None
    for tagelem in elem.iter("tag"):
        if is_housenum(tagelem):
            housenum_elem = tagelem
        if is_street_name(tagelem):
            housenum, fixed_name = fix_street_name(
                tagelem.attrib['v'], street_mapping) 
            tagelem.attrib['v'] = fixed_name
        if is_city_name(tagelem):
            fixed_name = fix_city_name(tagelem.attrib['v'], city_mapping) 
            tagelem.attrib['v'] = fixed_name
    if not housenum is None:
        if housenum_elem is None:
            hnattrib = {'k': "addr:housenumber", 'v': housenum}
            hn = ET.SubElement(elem, 'tag', hnattrib)
            print "house number attribute = ", hnattrib
            print "hn.attrib['v'] = ", hn.attrib['v']
        else:
            housenum_elem['v'] = housenum
    return elem

def audit_clean_addresses(fname, cleanup=False):
    city_names = set()
    rare_street_types = defaultdict(set)
    postcodes = defaultdict(set)
    with open(fname, "r") as osm_file:
        for event, elem in ET.iterparse(osm_file, events=("start",)):
            if elem.tag == "node" or elem.tag == "way":
                if cleanup:
                    elem = clean_address(elem)
                for tagelem in elem.iter("tag"):
                    if is_street_name(tagelem):
                        audit_street_type(tagelem.attrib['v'], 
                                          rare_street_types)   
                    if is_city_name(tagelem):
                        audit_city_name(tagelem.attrib['v'], city_names)
                    if is_postcode(tagelem):
                        audit_postcode(tagelem, postcodes)


    pprint.pprint(dict(rare_street_types))
    pprint.pprint(city_names)
    pprint.pprint(postcodes)

def audit_addresses(fname):
    print "\nAuditing addresses"
    print "=========================================================="
    audit_clean_addresses(fname, False)

def clean_addresses(fname):
    print "\nCleaning addresses"
    print "=========================================================="
    audit_clean_addresses(fname, True)

############################################################################
# Reshape data
############################################################################
"""
Wrangle the data and transform the shape of the data into the model we
mentioned earlier. The output should be a list of dictionaries that
look like this:

{
"id": "2406124091",
"type: "node",
"visible":"true",
"created": {
          "version":"2",
          "changeset":"17206049",
          "timestamp":"2013-08-03T16:43:42Z",
          "user":"linuxUser16",
          "uid":"1219059"
        },
"pos": [41.9757030, -87.6921867],
"address": {
          "housenumber": "5157",
          "postcode": "60625",
          "street": "North Lincoln Ave"
        },
"amenity": "restaurant",
"cuisine": "mexican",
"name": "La Cabana De Don Luis",
"phone": "1 (773)-271-5176"
}

You have to complete the function 'shape_element'.  We have provided a
function that will parse the map file, and call the function with the
element as an argument. You should return a dictionary, containing the
shaped data for that element.  We have also provided a way to save the
data in a file, so that you could use mongoimport later on to import
the shaped data into MongoDB.

Note that in this exercise we do not use the 'update street name'
procedures you worked on in the previous exercise. If you are using
this code in your final project, you are strongly encouraged to use
the code from previous exercise to update the street names before you
save them to JSON.

In particular the following things should be done:
- you should process only 2 types of top level tags: "node" and "way"
- all attributes of "node" and "way" should be turned into regular
  key/value pairs, except:
    - attributes in the CREATED array should be added under a key "created"
    - attributes for latitude and longitude should be added to a "pos" array,
      for use in geospacial indexing. Make sure the values inside
      "pos" array are floats and not strings.

- if second level tag "k" value contains problematic characters, it
  should be ignored

- if second level tag "k" value starts with "addr:", it should be
  added to a dictionary "address"

- if second level tag "k" value does not start with "addr:", but
  contains ":", you can process it same as any other tag.

- if there is a second ":" that separates the type/direction of a
  street, the tag should be ignored, for example:
<tag k="addr:housenumber" v="5158"/>
<tag k="addr:street" v="North Lincoln Avenue"/>
<tag k="addr:street:name" v="Lincoln"/>
<tag k="addr:street:prefix" v="North"/>
<tag k="addr:street:type" v="Avenue"/>
<tag k="amenity" v="pharmacy"/>
  should be turned into:

{...
"address": {
    "housenumber": 5158,
    "street": "North Lincoln Avenue"
}
"amenity": "pharmacy",
...
}

- for "way" specifically:

  <nd ref="305896090"/>
  <nd ref="1719825889"/>

should be turned into
"node_refs": ["305896090", "1719825889"]
"""
lower = re.compile(r'^([a-z]|_)*$')
lower_colon = re.compile(r'^([a-z]|_)*:([a-z]|_)*$')
problemchars = re.compile(r'[=\+/&<>;\'"\?%#$@\,\. \t\r\n]')
CREATED = ["version", "changeset", "timestamp", "user", "uid"]

def is_valid(element):
    valid = False
    if element.tag != "node" or element.tag != "way":
        # We can't trust an element unless it has a user attribute
        if get_user(element):
            valid = True
    return valid

def shape_element(element):
    node = {}
    # process only 2 types of top level tags: "node" and "way"
    if element.tag == "node" or element.tag == "way" :
        node['type'] = element.tag

        # Reshape attributes
        created = None
        pos = None
        node_refs = None
        for attr in element.attrib:
            val = element.attrib[attr]
            # attributes in the CREATED array should be added under a
            # key "created"
            if attr in CREATED:
                if created is None:
                    created = {}
                created[attr] = val
            # attributes for latitude and longitude should be added to
            # a "pos" array of floats
            elif attr in ['lat', 'lon']:
                if pos is None:
                    pos = [0, 0]
                idx = 0 if attr == 'lat' else 1
                pos[idx] = float(val)
            # shape all other attributes into regular key/value pairs
            else:
                node[attr] = val

        # Reshape second-level element
        address = None
        node_refs = None
        for child in element.iter():
            child = clean_address(child)
            if 'k' in child.attrib:
                key = child.attrib['k']
                val = child.attrib['v']

                # if second level tag "k" value contains problematic 
                # characters, it should be ignored
                if problemchars.search(key):
                    continue
                # if second level tag "k" value starts with "addr:", it 
                # should be added to a dictionary "address"
                if key.startswith("addr:"):
                    if address is None:
                        address = {}
                    l2key = key[len("addr:"):]
                    # if there is a second ":" that separates the
                    # type/direction of a street, the tag should be
                    # ignored, for example:
                    if not lower_colon.search(l2key):
                        address[l2key]=val
                # if second level tag "k" value does not start with
                # "addr:", but contains ":", you can process it same
                # as any other tag.
                else: 
                    node[key]=val

            # Turn <nd> elements inside a "way" into node_refs array
            if element.tag=='way' and child.tag=="nd" and 'ref' in child.attrib:
                if node_refs is None:
                    node_refs = []
                node_refs.append(child.attrib['ref'])
        if not created is None:
            node["created"]=created
        if not pos is None:
            node["pos"]=pos
        if not address is None:
            node["address"]=address
        if not node_refs is None:
            node["node_refs"]=node_refs
        return node
    else:
        return None

def test_reshaped_data(data):
    #pprint.pprint(data[0])
    correct_first_elem = {
        "id": "261114295", 
        "visible": "true", 
        "type": "node", 
        "pos": [41.9730791, -87.6866303], 
        "created": {
            "changeset": "11129782", 
            "user": "bbmiller", 
        "version": "7", 
            "uid": "451048", 
            "timestamp": "2012-03-28T18:31:23Z"
        }
    }
    assert data[0] == correct_first_elem
    assert data[-1]["address"] == {
        "street": "West Lexington St.", 
        "housenumber": "1412"
    }
    assert data[-1]["node_refs"] == [ "2199822281", "2199822390",  
                                      "2199822392", "2199822369", 
                                      "2199822370", "2199822284", 
                                      "2199822281"]

# Reshape and write data into a json file
def reshape_data(fname, pretty = False):
    print "\nReshaping and saving data"
    print "=========================================================="
    # You do not need to change this file
    file_out = "{0}.json".format(os.path.basename(fname))
    data = []
    with codecs.open(file_out, "w") as fo:
        for _, element in ET.iterparse(fname):
            shaped_elem = shape_element(element) if is_valid(element) else None
            if not shaped_elem is None:
                data.append(shaped_elem)
                if pretty:
                    fo.write(json.dumps(shaped_elem, indent=2)+"\n")
                else:
                    fo.write(json.dumps(shaped_elem) + "\n")

    # Test reshaped data
    if fname.endswith("example.osm"):
        test_reshaped_data(data)
    return data

# Insert maps data into database
def insert_maps(map_data, db):
    print "\nInserting data into MongoDB"
    print "=========================================================="
    for map_elem in map_data:
        db.maps.insert(map_elem)
    print "First document inserted into the maps database with {} documents:".format(db.maps.count())
    pprint.pprint(db.maps.find_one())

# Perform some queries in the maps database
def query_data(db):
    print "\nPerform queries on MongoDB"
    print "=========================================================="

    # number of unique users 
    uniq_user_count = len(db.maps.distinct("created.user"))
    print "There are {} of unique contrbuting users in Kolkata, India.".format(uniq_user_count)

    # number of nodes and ways 
    node_count = db.maps.find({"type" : "node"}).count()
    ways_count = db.maps.find({"type" : "way"}).count()
    print "There are {} nodes and {} ways in Kolkata, India ".format(node_count, ways_count)

    # number of chosen type of nodes, like cafes, shops etc 
    cafe_count = db.maps.find({"amenity" : "cafe"}).count()
    restaurant_count = db.maps.find({"amenity" : "restaurant"}).count()
    shop_count = db.maps.find({"amenity" : "shop"}).count()
    hospital_count = db.maps.find({"amenity" : "hospital"}).count()
    school_count = db.maps.find({"amenity" : "school"}).count()
    college_count = db.maps.find({"amenity" : "college"}).count()
    univ_count = db.maps.find({"amenity" : "university"}).count()

    print """Amenities:
        cafes: {}
        restaurants: {}
        shops: {}
        hospitals: {}
        schools: {}
        colleges: {}
        universities: {}
    """.format(cafe_count, restaurant_count, shop_count, hospital_count, school_count, college_count, univ_count)

    # Top 10 businesses
    pipeline = [
        {"$match" : {"type" : "node",
                     "shop" : {"$exists" : 1}}},
        {"$group" : {"_id" : "$shop",
                     "count" : {"$sum" : 1}}},
        {"$sort" : {"count" : -1}},
        {"$limit" : 10}
    ]
    result = db.maps.aggregate(pipeline)
    print "Top 10 businesses:"
    pprint.pprint(result)

    # Number of different types of highways
    pipeline = [
        {"$match" : {"type" : "way",
                     "highway" : {"$exists" : 1}}},
        {"$group" : {"_id" : "$highway",
                     "count" : {"$sum" : 1}}},
        {"$sort" : {"count" : -1}},
        {"$limit" : 10}
    ]
    result = db.maps.aggregate(pipeline)
    print "Number and types of highways"
    pprint.pprint(result)


    db.maps.aggregate

def wrangle_maps(fname):
    # Audit some data elements
    audit_tags(fname)
    audit_keys(fname)
    audit_users(fname)
    audit_addresses(fname)

    # Clean up addresses and save to a json file
    clean_addresses(fname)

    # Reshape and write data into a json file
    map_data = reshape_data(fname, True)
    pprint.pprint(map_data[0])

    # Insert maps data into database
    db = get_mongodb('maps')
    insert_maps(map_data, db) 
    #db.maps.stats()

    # release memory held by map_data; we will work with mongodb from now on
    del map_data

    # Perform some queries in the maps database
    query_data(db)

    # Clean up data
    db.maps.drop()

DATADIR = "../../../datasets/"
EXAMPLE_OSMFILE =  "example.osm"
CHICAGO_OSMFILE = "chicago.osm"
#KOLKATA_OSMFILE =  "kolkata_india.osm.bz2"
KOLKATA_OSMFILE =  "kolkata_india.osm"
SAMPLE_OSMFILE = "sample.osm"

USE_SAMPLE_DATA = True

if __name__ == '__main__':
    if USE_SAMPLE_DATA:
        fname = find_file("./", SAMPLE_OSMFILE)
    else:
        fname = find_file(DATADIR, KOLKATA_OSMFILE)
        # optionally produce sample file
        #sample_elements(fname, SAMPLE_OSMFILE)

    wrangle_maps(fname)

