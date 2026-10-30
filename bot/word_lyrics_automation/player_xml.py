"""Role-aware TTML projection for independent player response validation."""
import math,re
import xml.etree.ElementTree as ET
TTM="http://www.w3.org/ns/ttml#metadata"

def local_name(value):
    return value.rsplit("}", 1)[-1]


def time_value(expression):
    text = expression.strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", text)
    if match:
        multiplier = {"ms": .001, "s": 1, "m": 60, "h": 3600}[match[2]]
        return math.floor(float(match[1]) * multiplier * 1000 + .5), "offset"
    if ":" in text:
        total = 0.0
        for component in text.split(":"):
            total = total * 60 + float(component)
        return math.floor(total * 1000 + .5), "absolute"
    return math.floor(float(text) * 1000 + .5), "ambiguous"


def resolve_time(expression, base, parent):
    value, kind = time_value(expression)
    if kind == "absolute":
        return value
    if kind == "offset":
        return base + value
    begin, end = parent
    offset = base + value
    if begin is None and end is None and base != 0:
        return value
    if begin is not None and end is not None:
        absolute_inside = begin <= value <= end
        offset_inside = begin <= offset <= end
        if absolute_inside and not offset_inside:
            return value
        if offset_inside and not absolute_inside:
            return offset
    if begin is not None:
        if value < begin <= offset:
            return offset
        if value >= begin and offset > value:
            return value
    return offset


def context(element, parent):
    begin, end = parent
    if "begin" in element.attrib:
        begin = resolve_time(element.attrib["begin"], parent[0] or 0, parent)
    if "end" in element.attrib:
        end = resolve_time(element.attrib["end"], begin or parent[0] or 0, parent)
    if "dur" in element.attrib and begin is not None:
        duration, _ = time_value(element.attrib["dur"])
        calculated = begin + duration
        end = calculated if "end" not in element.attrib else min(end, calculated)
    elif "dur" in element.attrib and local_name(element.tag) == "body":
        duration, _ = time_value(element.attrib["dur"])
        end = duration
    return begin, end


def source_groups(text):
    root=ET.fromstring(text)
    result=[];display=[]

    def walk(element,parent,inherited_agent=None):
        timing=context(element,parent)
        agent=element.get('{'+TTM+'}agent',inherited_agent)
        if local_name(element.tag)!='p':
            for child in element:
                if local_name(child.tag) not in {'head','metadata'}:
                    walk(child,timing,agent)
            return
        index=len(display)
        pieces=[]

        def inline(node,parent_timing,role_agent):
            node_timing=context(node,parent_timing)
            node_agent=node.get('{'+TTM+'}agent',role_agent[1])
            node_role=node.get('{'+TTM+'}role','')
            group=('background',node_agent) if node_role=='x-bg' else (role_agent[0],node_agent)
            timed=any(key in node.attrib for key in ['begin','end','dur'])
            if local_name(node.tag)=='span' and timed and not list(node):
                pieces.append({'text':node.text or '', 'timing':node_timing,'group':group})
                return
            if node.text:
                pieces.append({'text':node.text,'timing':None,'group':group})
            for child in node:
                inline(child,node_timing,group)
                if child.tail:
                    pieces.append({'text':child.tail,'timing':None,'group':group})

        if element.text:
            pieces.append({'text':element.text,'timing':None,'group':('main',agent)})
        for child in element:
            inline(child,timing,('main',agent))
            if child.tail:
                pieces.append({'text':child.tail,'timing':None,'group':('main',agent)})
        all_text=re.sub(r'[ \t\r\n]+',' ',''.join(piece['text'] for piece in pieces)).strip()
        display.append({'start':timing[0],'value':all_text})
        ordered_groups=list(dict.fromkeys(piece['group'] for piece in pieces))
        for group in ordered_groups:
            emitted='';tokens=[]
            for piece in pieces:
                if piece['group']!=group:continue
                raw=re.sub(r'[ \t\r\n]+',' ',piece['text'])
                if not emitted or emitted.endswith(' '):raw=raw.removeprefix(' ')
                if not raw:continue
                byte_start=len(emitted.encode('utf8'));emitted+=raw
                if piece['timing']:
                    tokens.append({'start':piece['timing'][0],'end':piece['timing'][1],
                                   'byteStart':byte_start,'byteEnd':len(emitted.encode('utf8'))-1,'value':raw})
            emitted=emitted.rstrip()
            if not emitted:continue
            for token in tokens:
                if token['byteEnd']>=len(emitted.encode('utf8')):
                    token['byteEnd']=len(emitted.encode('utf8'))-1
                    token['value']=emitted.encode('utf8')[token['byteStart']:token['byteEnd']+1].decode('utf8')
            result.append({'index':index,'start':timing[0],'end':timing[1],'value':emitted,
                           'agentId':('__nd_bg__|' if group[0]=='background' else '')+(group[1] or ''),'cue':tokens})

    walk(root,(None,None))
    return display,result
