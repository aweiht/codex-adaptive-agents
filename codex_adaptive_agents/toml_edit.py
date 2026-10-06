# Surgical managed-scalar editing. Input and parse errors never reach logs.
import sys,json,tomllib,re
TYPES={'model':str,'model_reasoning_effort':str,'agents.enabled':bool,'features.hooks':bool,
 'agents.max_concurrent_threads_per_session':int,'agents.max_threads':int,
 'agents.default_subagent_model':str,'agents.default_subagent_reasoning_effort':str}
MANAGED=set(TYPES)
def parsed(raw):return tomllib.loads(raw)

def scalar_value(literal,key):
 if key not in TYPES or not isinstance(literal,str) or '\n' in literal or '\r' in literal:raise ValueError('invalid managed scalar')
 try:obj=parsed('v='+literal)
 except Exception:raise ValueError('invalid managed scalar') from None
 if not isinstance(obj,dict) or set(obj)!={'v'}:raise ValueError('invalid managed scalar')
 value=obj['v']
 if type(value) is not TYPES[key]:raise ValueError('invalid managed scalar')
 return value

def lookup(obj,path):
 for part in path.split('.'):
  if not isinstance(obj,dict) or part not in obj:return None
  obj=obj[part]
 return obj

def statements(text):
 start=0;i=0;quote=None;depth=0;comment=False
 while i<len(text):
  c=text[i]
  if comment:
   if c=='\n':comment=False
   else:i+=1;continue
  if quote:
   if quote in ('"','"""') and c=='\\':i+=2;continue
   if text.startswith(quote,i):i+=len(quote);quote=None;continue
   i+=1;continue
  if c=='#':comment=True;i+=1;continue
  if c in "\"'":
   quote=c*3 if text.startswith(c*3,i) else c;i+=len(quote);continue
  if c in '[{':depth+=1
  elif c in ']}':depth-=1
  if c=='\n' and depth==0:yield start,i+1,text[start:i+1];start=i+1
  i+=1
 if start<len(text):yield start,len(text),text[start:]

def path_of(obj):
 p=[]
 while isinstance(obj,dict) and len(obj)==1:
  k,obj=next(iter(obj.items()));p.append(k)
 return p

def inline_parts(text,open_at):
 parts=[];start=open_at+1;i=start;depth=1;quote=None
 while i<len(text):
  c=text[i]
  if quote:
   if c=='\\' and quote=='"':i+=2;continue
   if c==quote:quote=None
  elif c in "\"'":quote=c
  elif c in '[{':depth+=1
  elif c in ']}':
   depth-=1
   if depth==0:parts.append((start,i));return parts,i
  elif c==',' and depth==1:parts.append((start,i));start=i+1
  i+=1
 raise ValueError('inline table span invalid')

def inline_locations(raw):
 locations={};tables={};section=[]
 for start,end,s in statements(raw):
  line=s.lstrip()
  if line.startswith('['):section=['table'];continue
  if section or '=' not in s:continue
  eq=s.index('=')
  try:key=path_of(parsed(s[:eq]+'=1'))
  except Exception:continue
  if len(key)!=1 or key[0] not in ('agents','features'):continue
  rest=s[eq+1:];offset=eq+1+len(rest)-len(rest.lstrip())
  if offset>=len(s) or s[offset]!='{':continue
  parts,closing=inline_parts(s,offset);tables[key[0]]=(start+closing,bool(lookup(parsed(raw),key[0])))
  for index,(lo,hi) in enumerate(parts):
   part=s[lo:hi]
   if '=' not in part:continue
   peq=part.index('=');leaf=path_of(parsed(part[:peq]+'=1'));full='.'.join(key+leaf)
   if full not in MANAGED:continue
   value=part[peq+1:];left=len(value)-len(value.lstrip());right=len(value.rstrip());literal=value.strip()
   a=start+lo+peq+1+left;b=start+lo+peq+1+right
   remove_a=start+lo if index<len(parts)-1 or index==0 else start+lo-1
   remove_b=start+hi+1 if index<len(parts)-1 else start+hi
   locations[full]=(remove_a,remove_b,a,b,literal)
 return locations,tables

def managed_locations(raw):
 loc={};section=[]
 for start,end,s in statements(raw):
  line=s.lstrip()
  if not line or line.startswith('#'):continue
  if line.startswith('[['):section=['__array_table__'];continue
  if line.startswith('['):
   # TOML's own parser handles quoted/dotted section names.
   section=path_of(parsed(s+'\n__orchestrator_marker__=1\n'))[:-1];continue
  quote=None;eq=None
  for i,c in enumerate(s):
   if quote:
    if c==quote:quote=None
   elif c in "\"'":quote=c
   elif c=='=':eq=i;break
  if eq is None:continue
  key_parts=path_of(parsed(s[:eq]+'=1'))
  key='.'.join(section+key_parts)
  if key not in MANAGED:continue
  # All owned values are scalar; their source span is found without swallowing
  # a trailing comment (including a # inside a quoted model name).
  tail=s[eq+1:];q=None;escaped=False;stop=len(tail)
  for i,c in enumerate(tail):
   if escaped:escaped=False;continue
   if q:
    if c=='\\' and q=='"':escaped=True
    elif c==q:q=None
   elif c in "\"'":q=c
   elif c in '#\r\n':stop=i;break
  literal=tail[:stop].strip()
  if '\n' in literal or type(lookup(parsed(raw),key)) not in (str,bool,int):raise ValueError('unsupported managed value')
  left=len(tail[:stop])-len(tail[:stop].lstrip());right=len(tail[:stop].rstrip())
  loc[key]=(start,end,start+eq+1+left,start+eq+1+right,literal)
 inline,_=inline_locations(raw);loc.update(inline)
 return loc

def set_value(raw,key,literal):
 if key not in MANAGED:raise ValueError('unmanaged key')
 if literal is not None:scalar_value(literal,key)
 obj=parsed(raw);loc=managed_locations(raw)
 if key in loc:
  start,end,a,b,old=loc[key]
  if literal is None:
   inline,_=inline_locations(raw)
   if key not in inline:
    suffix=raw[b:end].lstrip()
    if suffix.startswith('#'):return raw[:start]+suffix+raw[end:]
   return raw[:start]+raw[end:]
  return raw[:a]+literal+raw[b:]
 if lookup(obj,key) is not None:raise ValueError('managed key is inside unsupported syntax')
 if literal is None:return raw
 if '.' not in key:return key+' = '+literal+'\n'+raw
 parent,leaf=key.split('.')
 _,inline_tables=inline_locations(raw)
 if parent in inline_tables:
  pos,nonempty=inline_tables[parent]
  return raw[:pos]+(', ' if nonempty else '')+leaf+' = '+literal+raw[pos:]
 # Existing ordinary table: insert immediately after its section header.
 for start,end,s in statements(raw):
  if s.lstrip().startswith('[') and not s.lstrip().startswith('[['):
   section=path_of(parsed(s+'\n__orchestrator_marker__=1\n'))[:-1]
   if section==[parent]:return raw[:end]+leaf+' = '+literal+'\n'+raw[end:]
 # A parent can be implicit from dotted root keys or a child table. Try a
 # legal scalar insertion; the TOML parser rejects table redefinition.
 if parent in obj:
  candidates=[key+' = '+literal+'\n'+raw,
              raw+('' if raw.endswith('\n') else '\n')+'\n['+parent+']\n'+leaf+' = '+literal+'\n']
  for candidate in candidates:
   try:
    updated=parsed(candidate)
    if lookup(updated,key)==parsed('v='+literal)['v']:return candidate
   except ValueError:pass
  raise ValueError('managed table cannot be safely extended')
 return raw+('' if raw.endswith('\n') or not raw else '\n')+'\n['+parent+']\n'+leaf+' = '+literal+'\n'

def remove_created_tables(raw,tables):
 """Remove only installer-created, still-empty ordinary tables.

 The merge path records the exact LF separator it added before a new table.
 Keep a changed header, comment, value, or unrelated section untouched; old
 receipts without this metadata therefore remain conservative by design.
 """
 if not tables:return raw
 separators={}
 for row in tables:
  if not isinstance(row,dict):raise ValueError('invalid created table receipt')
  name=row.get('name');separator=row.get('separator')
  if (name not in ('agents','features') or name in separators or
      type(separator) is not int or separator not in (1,2)):
   raise ValueError('invalid created table receipt')
  separators[name]=separator
 rows=list(statements(raw));spans=[]
 for index,(start,end,s) in enumerate(rows):
  line=s.lstrip()
  if not line.startswith('[') or line.startswith('[['):continue
  try:section=path_of(parsed(s+'\n__orchestrator_marker__=1\n'))[:-1]
  except Exception:continue
  if len(section)!=1 or section[0] not in separators or s.strip('\r\n')!='['+section[0]+']':continue
  next_start=len(raw)
  for next_start_candidate,_,next_statement in rows[index+1:]:
   if next_statement.lstrip().startswith('['):
    next_start=next_start_candidate;break
  if raw[end:next_start].strip():continue
  separator=separators[section[0]]
  cut=start-separator
  if cut<0 or raw[cut:start]!='\n'*separator:continue
  spans.append((cut,end))
 for start,end in reversed(spans):raw=raw[:start]+raw[end:]
 return raw

def run(req):
 raw=req['input']
 if not isinstance(raw,str):raise ValueError('TOML input must be text')
 mode=req.get('mode')
 if mode not in ('merge','revert'):raise ValueError('unsupported TOML edit mode')
 obj=parsed(raw);locations=managed_locations(raw);changes=[];conflicts=[];created_tables=[]
 if mode=='merge':
  desired=req.get('desired',{})
  if not isinstance(desired,dict):raise ValueError('invalid desired managed values')
  validated=[]
  for key,value in sorted(desired.items()):
   if key not in MANAGED:raise ValueError('unmanaged key')
   actual=scalar_value(value,key)
   if TYPES[key] is int and actual<1:raise ValueError('invalid native concurrency')
   validated.append((key,value,actual))
  for key,value,actual in validated:
   current=lookup(obj,key)
   if current is not None and type(current) is not TYPES[key]:raise ValueError('wrong managed scalar type')
   if current==actual:continue
   before=locations[key][4] if key in locations else None
   if '.' in key:
    parent=key.split('.',1)[0]
    if parent not in obj and parent not in {row['name'] for row in created_tables}:
     created_tables.append({'name':parent,'separator':1 if not raw or raw.endswith('\n') else 2})
   raw=set_value(raw,key,value);parsed(raw)
   changes.append({'key':key,'before':before,'after':value})
 else:
  requested=req.get('changes',[])
  if not isinstance(requested,list):raise ValueError('invalid managed changes')
  validated=[];seen=set();current_obj=parsed(raw)
  for ch in requested:
   if not isinstance(ch,dict):raise ValueError('invalid managed change')
   key=ch['key']
   if key not in MANAGED or key in seen:raise ValueError('unmanaged or duplicate receipt key')
   seen.add(key)
   expected=scalar_value(ch['after'],key)
   before=ch.get('before')
   if before is not None:scalar_value(before,key)
   current=lookup(current_obj,key)
   if type(current) is not type(expected) or current!=expected:conflicts.append(key)
   validated.append((ch,key,before))
  if conflicts:return {'output':raw,'changes':[],'conflicts':conflicts,'created_tables':[]}
  for ch,key,before in validated:
   try:raw=set_value(raw,key,ch['before']);parsed(raw)
   except ValueError:conflicts.append(key)
  if conflicts:return {'output':req['input'],'changes':[],'conflicts':conflicts,'created_tables':[]}
 parsed(raw);return {'output':raw,'changes':changes,'conflicts':conflicts,'created_tables':created_tables}
if __name__ == '__main__':
 try:
  req=json.load(sys.stdin);result=run(req);json.dump(result,sys.stdout)
 except Exception:
  sys.stdout.write('{"error":"TOML parse or safe managed-key merge failed"}');sys.exit(1)
