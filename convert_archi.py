"""Convert your Archi-native project (.archimate) into the Open Exchange format the
ArchiMate MCP server reads, keeping the WHOLE project: every element, every
relationship and every view (AS-IS, TO-BE, TOGAF...). Nothing is corrected or removed.

Preferred alternative: do it yourself in Archi with
    File > Export > Model to Open Exchange File...
and save the result as models/vrio.archimate. Both routes give a file the server can load.

Usage:
    pip install archimate-mcp
    python convert_archi.py source/Vrio.archimate models/vrio.archimate
"""
import sys
import xml.etree.ElementTree as ET

from archimate_mcp.api.api_client_archi import ArchiApi

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


def kind(el):
    return el.get(XSI_TYPE, "").split(":")[-1]


def walk(node, ox=0, oy=0):
    """Yield (child, absolute_x, absolute_y) for every diagram object, nested ones included."""
    for child in node.findall("child"):
        b = child.find("bounds")
        x = ox + int(float(b.get("x", 0))) if b is not None else ox
        y = oy + int(float(b.get("y", 0))) if b is not None else oy
        yield child, x, y, b
        yield from walk(child, x, y)


def main(src, dst):
    root = ET.parse(src).getroot()
    api = ArchiApi(model_path=dst, autoload=False)
    api.new_model(root.get("name", "Model"))

    elements, relationships, views = {}, [], []
    for el in root.iter("element"):
        k = kind(el)
        if k.endswith("Relationship"):
            relationships.append(el)
        elif k.endswith("DiagramModel"):
            views.append(el)
        else:
            elements[el.get("id")] = el

    emap, rmap = {}, {}
    for eid, el in elements.items():
        emap[eid] = api.add_element(kind(el), el.get("name", ""))
    for rel in relationships:
        s, t = rel.get("source"), rel.get("target")
        if s in emap and t in emap:
            rmap[rel.get("id")] = api.add_relationship(
                kind(rel).removesuffix("Relationship"), emap[s], emap[t], validate=False)

    for view in views:
        vid = api.create_view(view.get("name", "View"))
        conns = []
        for child, x, y, b in walk(view):
            ref = child.get("archimateElement")
            if ref in emap:
                w = int(float(b.get("width", 120))) if b is not None else 120
                h = int(float(b.get("height", 55))) if b is not None else 55
                api.add_element_to_view(vid, emap[ref], x, y, w, h)
            conns += [c.get("archimateRelationship") for c in child.iter("sourceConnection")]
        for r in dict.fromkeys(conns):
            if r in rmap:
                api.add_connection_to_view(vid, rmap[r])

    api.save()
    print(f"{dst}: {len(emap)} elements, {len(rmap)} relationships, {len(views)} views")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
