from __future__ import annotations
import hashlib, hmac, json, mimetypes, os, secrets, socket, sqlite3, subprocess, time, urllib.error, urllib.parse, urllib.request
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT=Path(__file__).resolve().parent; WEB=ROOT/"web"
COMFY_ROOT=Path(os.environ.get('COMFY_ROOT',ROOT.parent/'ComfyUI')).resolve()
INPUT=COMFY_ROOT/"input"/"theodore_mobile"; OUTPUT=COMFY_ROOT/"output"
COMFY=os.environ.get('COMFY_URL','http://127.0.0.1:8188').rstrip('/');PORT=int(os.environ.get('DIRECTOR_PORT','8190'))
API_DIR=Path(os.environ.get('WORKFLOW_DIR',ROOT.parent/'workflows')).resolve();JOBS=ROOT/"jobs.json";AUTH_DB=ROOT/"auth.db";THUMBS=ROOT/".thumbs"
FFMPEG=Path(os.environ.get('FFMPEG_PATH','ffmpeg'))
INPUT.mkdir(parents=True,exist_ok=True)
THUMBS.mkdir(exist_ok=True)
LOGIN_ATTEMPTS={}
def db():
 con=sqlite3.connect(AUTH_DB,timeout=10);con.row_factory=sqlite3.Row
 con.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE COLLATE NOCASE,password BLOB NOT NULL,salt BLOB NOT NULL,role TEXT NOT NULL DEFAULT 'user',created_at INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
 CREATE TABLE IF NOT EXISTS invites(code_hash TEXT PRIMARY KEY,max_uses INTEGER NOT NULL DEFAULT 1,uses INTEGER NOT NULL DEFAULT 0,expires_at INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_by INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS daily_usage(user_id INTEGER NOT NULL,day TEXT NOT NULL,videos INTEGER NOT NULL DEFAULT 0,images INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,day));''')
 return con
def password_hash(password,salt):return hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
def user_input(user):
 root=INPUT if user['role']=='admin' else COMFY_ROOT/'input'/(f"theodore_public/u{user['id']}" if user['role']=='public' else f"theodore_users/u{user['id']}");root.mkdir(parents=True,exist_ok=True);return root
def input_prefix(user):return 'theodore_mobile' if user['role']=='admin' else f"theodore_public/u{user['id']}" if user['role']=='public' else f"theodore_users/u{user['id']}"
def result_root(user):
 root=OUTPUT if user['role']=='admin' else OUTPUT/(f"TheodorePublic/u{user['id']}" if user['role']=='public' else f"TheodoreUsers/u{user['id']}");root.mkdir(parents=True,exist_ok=True);return root
def output_prefix(user):return 'TheodoreMobile' if user['role']=='admin' else f"TheodorePublic/u{user['id']}" if user['role']=='public' else f"TheodoreUsers/u{user['id']}"
def usage(user_id):
 day=time.strftime('%Y-%m-%d');con=db();row=con.execute('SELECT videos,images FROM daily_usage WHERE user_id=? AND day=?',(user_id,day)).fetchone();con.close();return {'videos':row['videos'] if row else 0,'images':row['images'] if row else 0,'videoLimit':5,'imageLimit':10}
def consume(user,media,count=1):
 if user['role'] in {'admin','public'}:return
 u=usage(user['id']);key='videos' if media=='video' else 'images';limit=u['videoLimit' if media=='video' else 'imageLimit']
 if u[key]+count>limit:raise ValueError(f"今日{('视频' if media=='video' else '图片')}额度不足（{u[key]}/{limit}）")
 day=time.strftime('%Y-%m-%d');con=db();con.execute('INSERT INTO daily_usage(user_id,day,videos,images) VALUES(?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET '+key+'='+key+'+excluded.'+key,(user['id'],day,count if key=='videos' else 0,count if key=='images' else 0));con.commit();con.close()
def ensure_quota(user,media,count=1):
 if user['role'] in {'admin','public'}:return
 u=usage(user['id']);key='videos' if media=='video' else 'images';limit=u['videoLimit' if media=='video' else 'imageLimit']
 if u[key]+count>limit:raise ValueError(f"今日{('视频' if media=='video' else '图片')}额度不足（{u[key]}/{limit}）")
def lan_ip():
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
 try:s.connect(("8.8.8.8",80));return s.getsockname()[0]
 except OSError:return "电脑IPv4"
 finally:s.close()
def kind(p):
 e=p.suffix.lower();return "image" if e in {'.png','.jpg','.jpeg','.webp','.bmp'} else "video" if e in {'.mp4','.webm','.mov','.mkv'} else "audio" if e in {'.mp3','.wav','.flac','.m4a','.ogg'} else None
def jobs():
 try:return json.loads(JOBS.read_text(encoding='utf-8'))
 except Exception:return {}
def save_jobs(data):JOBS.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
def group_info(req):
 core={k:req.get(k) for k in ('mode','kind','prompt','negative','selected','firstFrame','lastFrame','aspect','resolution','duration','shots','editMode')}
 gid=hashlib.sha1(json.dumps(core,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:12]
 title=(req.get('title') or ('Qwen 图片编辑' if req.get('kind') else req.get('mode','生成结果'))).strip()
 safe=''.join(c if c.isalnum() or c in '-_' else '_' for c in title)[:36].strip('_') or 'project'
 return gid,safe
def comfy_post(path,payload):
 req=urllib.request.Request(COMFY+path,data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='POST')
 with urllib.request.urlopen(req,timeout=30) as r:
  data=r.read();return json.loads(data) if data else {}
def comfy_get(path):
 with urllib.request.urlopen(COMFY+path,timeout=30) as r:return json.loads(r.read())
def workflow(req,user):
 mode=req.get('mode','t2v'); src=API_DIR/f"minimax_h3_{mode}_api.json"
 if not src.is_file():raise ValueError('缺少已导出的官方 API 工作流，请重新从 ComfyUI 模板导出')
 graph=json.loads(src.read_text(encoding='utf-8')); duration=max(5,min(15,float(req.get('duration') or 5)))
 prompt=(req.get('prompt') or '').strip(); shots=[x for x in req.get('shots',[]) if str(x.get('text','')).strip()]
 if shots:
  cursor=0.0; lines=[]
  for i,shot in enumerate(shots,1):
   seconds=max(.5,float(shot.get('seconds') or 2));end=min(duration,cursor+seconds);lines.append(f"SHOT {i} [{cursor:g}s-{end:g}s]: {str(shot['text']).strip()}");cursor=end
  prompt=(prompt+'\n\nStoryboard (cut in order):\n'+'\n'.join(lines)).strip()
 gid,safe=group_info(req);prefix=f"{output_prefix(user)}/{safe}_{gid}/run_{int(time.time())}"
 graph['92']['inputs']['filename_prefix']=prefix
 for node in graph.values():
  if node['class_type']=='UNETLoader':node['inputs']['unet_name']='minimax_h3_ref2va_int8_convrot.safetensors' if mode=='r2v' else 'minimax_h3_fl2va_int8_convrot.safetensors'
  if node['class_type']=='CLIPLoader':node['inputs']['clip_name']='qwen3vl_32b_minimax_h3_int8_convrot.safetensors'
  if node['class_type']=='PrimitiveFloat':node['inputs']['value']=duration
  if node['class_type']=='RandomNoise':node['inputs']['noise_seed']=int(req.get('seed') or time.time_ns()%1125899906842624)
  if node['class_type']=='ResolutionSelector':
   allowed={'1:1 (Square)','2:3 (Portrait Photo)','3:2 (Photo)','3:4 (Portrait Standard)','4:3 (Standard)','9:16 (Portrait Widescreen)','16:9 (Widescreen)','21:9 (Ultrawide)'}
   node['inputs']['aspect_ratio']=req.get('aspect') if req.get('aspect') in allowed else '16:9 (Widescreen)'
   node['inputs']['megapixels']=max(.1,min(1.0,float(req.get('resolution') or .4)));node['inputs']['multiple']=32
 target=next(v for v in graph.values() if v['class_type'] in {'MiniMaxH3ImageToVideo','MiniMaxH3ReferenceToVideo'})
 target['inputs']['prompt']=prompt
 if mode=='i2v':
  first=Path(req.get('firstFrame','')).name
  if not first:raise ValueError('请选择首帧')
  next(v for v in graph.values() if v['class_type']=='LoadImage')['inputs']['image']=input_prefix(user)+'/'+first
  last=Path(req.get('lastFrame','')).name
  if last:
   graph['mobile_last']={'class_type':'LoadImage','inputs':{'image':input_prefix(user)+'/'+last}}
   target['inputs']['last_frame']=['mobile_last',0]
 if mode=='r2v':
  own=user_input(user);refs=[Path(x).name for x in req.get('selected',[]) if (own/Path(x).name).is_file()]
  images=[x for x in refs if kind(own/x)=='image'][:9]; videos=[x for x in refs if kind(own/x)=='video'][:3]
  if not images and not videos:raise ValueError('请选择至少一张参考图片或一个参考视频')
  # The official template ships with demo references. Remove them and their
  # dynamic inputs before wiring the materials selected in the mobile UI.
  for key in [k for k,v in graph.items() if v['class_type']=='LoadImage']:graph.pop(key)
  for key in [k for k in target['inputs'] if k.startswith(('ref_images.','ref_videos.','ref_video_audios.','ref_audios.'))]:target['inputs'].pop(key)
  aliases={Path(x).stem:i+1 for i,x in enumerate(images)}
  for i,name in enumerate(videos):aliases[Path(name).stem]=i+1
  for alias,index in aliases.items():
   tag=f'<Picture {index}>' if alias in {Path(x).stem for x in images} else f'<Video {index}>'
   target['inputs']['prompt']=target['inputs']['prompt'].replace('{{ref:'+alias+'}}',tag)
  for i,name in enumerate(images):
   nid=f'mobile_image_{i}';graph[nid]={'class_type':'LoadImage','inputs':{'image':input_prefix(user)+'/'+name}};target['inputs'][f'ref_images.ref_image_{i}']=[nid,0]
  for i,name in enumerate(videos):
   load=f'mobile_video_{i}';parts=f'mobile_parts_{i}'
   graph[load]={'class_type':'LoadVideo','inputs':{'file':input_prefix(user)+'/'+name}}
   graph[parts]={'class_type':'GetVideoComponents','inputs':{'video':[load,0]}}
   target['inputs'][f'ref_videos.ref_video_{i}']=[parts,0];target['inputs'][f'ref_video_audios.ref_video_audio_{i}']=[parts,1]
 return graph,prefix,duration
def qwen_workflow(req,user):
 src=API_DIR/'qwen_image_edit_2511_api.json'
 if not src.is_file():raise ValueError('缺少 Qwen Image Edit API 工作流')
 own=user_input(user);graph=json.loads(src.read_text(encoding='utf-8')); selected=[Path(x).name for x in req.get('selected',[]) if (own/Path(x).name).is_file() and kind(own/Path(x).name)=='image'][:3]
 if not selected:raise ValueError('请先在素材库选择至少一张图片')
 prompt=(req.get('prompt') or '').strip()
 for i,name in enumerate(selected,1):prompt=prompt.replace('{{ref:'+Path(name).stem+'}}',f'<Picture {i}>')
 gid,safe=group_info(dict(req,kind='qwen-image-edit'));prefix=f"{output_prefix(user)}/{safe}_{gid}/run_{int(time.time())}"
 graph['195']['inputs']['filename_prefix']=prefix
 graph['170:161']['inputs']['unet_name']='qwen_image_edit_2511_fp8_e4m3fn.safetensors'
 graph['170:162']['inputs']['clip_name']='qwen_2.5_vl_7b_fp8_scaled.safetensors'
 for nid in [k for k,v in graph.items() if v['class_type']=='LoadImage']:graph.pop(nid)
 pos=graph['170:151']['inputs'];neg=graph['170:149']['inputs'];pos['prompt']=prompt;neg['prompt']=req.get('negative','')
 for key in [k for k in pos if k.startswith('image')]:pos.pop(key)
 for key in [k for k in neg if k.startswith('image')]:neg.pop(key)
 first=None
 for i,name in enumerate(selected,1):
  nid=f'qwen_image_{i}';scale=f'qwen_scale_{i}';graph[nid]={'class_type':'LoadImage','inputs':{'image':input_prefix(user)+'/'+name}};graph[scale]={'class_type':'FluxKontextImageScale','inputs':{'image':[nid,0]}}
  pos[f'image{i}']=[scale,0];neg[f'image{i}']=[scale,0]
  if first is None:first=scale
 graph['170:156']['inputs']['pixels']=[first,0]
 graph.pop('170:160',None);graph['170:169']['inputs']['seed']=int(req.get('seed') or time.time_ns()%1125899906842624)
 return graph,prefix
class H(BaseHTTPRequestHandler):
 def log_message(self,f,*a):print("[mobile]",f%a)
 def out(self,n,b,t="application/json; charset=utf-8",headers=None):
  if not isinstance(b,bytes):b=json.dumps(b,ensure_ascii=False).encode()
  self.send_response(n);self.send_header("Content-Type",t);self.send_header("Content-Length",str(len(b)));self.send_header("Cache-Control","no-store")
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):return self.rfile.read(int(self.headers.get("Content-Length","0")))
 def current_user(self):
  try:
   c=SimpleCookie(self.headers.get('Cookie',''));token=c.get('td_session')
   if not token:return None
   con=db();row=con.execute('SELECT u.id,u.username,u.role,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',(hashlib.sha256(token.value.encode()).hexdigest(),int(time.time()))).fetchone();con.close();return row
  except Exception:return None
 def require_user(self):
  user=self.current_user()
  if not user:self.out(401,{'error':'请先登录'})
  return user
 def check_csrf(self,user):
  return bool(user and hmac.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']))
 def set_session(self,user_id):
  token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);expires=int(time.time())+7*86400;con=db();con.execute('DELETE FROM sessions WHERE expires_at<=?',(int(time.time()),));con.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,expires));con.commit();con.close()
  cookie=f'td_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={7*86400}'
  if os.environ.get('THEODORE_SECURE_COOKIE')=='1' or self.headers.get('X-Forwarded-Proto','').lower()=='https':cookie+='; Secure'
  return cookie,csrf
 def auth_post(self):
  try:req=json.loads(self.body() or b'{}')
  except Exception:return self.out(400,{'error':'请求格式错误'})
  if self.path=='/api/auth/register':
   username=str(req.get('username','')).strip();password=str(req.get('password',''));invite=str(req.get('invite','')).strip()
   if not (3<=len(username)<=32) or not all(c.isalnum() or c in '_-' for c in username):return self.out(400,{'error':'账号需为 3-32 位字母、数字、下划线或短横线'})
   if len(password)<10:return self.out(400,{'error':'密码至少 10 位'})
   con=db()
   try:
    count=con.execute('SELECT COUNT(*) FROM users').fetchone()[0];role='admin' if count==0 else 'user';invite_row=None
    if count:
     invite_row=con.execute('SELECT * FROM invites WHERE code_hash=? AND active=1 AND uses<max_uses AND expires_at>?',(hashlib.sha256(invite.encode()).hexdigest(),int(time.time()))).fetchone()
     if not invite_row:return self.out(400,{'error':'邀请码无效、已过期或已用完'})
    salt=secrets.token_bytes(16);cur=con.execute('INSERT INTO users(username,password,salt,role,created_at) VALUES(?,?,?,?,?)',(username,password_hash(password,salt),salt,role,int(time.time())))
    if invite_row:con.execute('UPDATE invites SET uses=uses+1 WHERE code_hash=?',(invite_row['code_hash'],))
    con.commit();uid=cur.lastrowid
   except sqlite3.IntegrityError:return self.out(409,{'error':'账号名已存在'})
   finally:con.close()
   cookie,csrf=self.set_session(uid);return self.out(200,{'ok':True,'role':role,'csrf':csrf},headers={'Set-Cookie':cookie})
  if self.path=='/api/auth/login':
   ip=self.headers.get('X-Forwarded-For',self.client_address[0]).split(',')[0].strip();now=int(time.time());attempts=[x for x in LOGIN_ATTEMPTS.get(ip,[]) if now-x<600]
   if len(attempts)>=8:return self.out(429,{'error':'登录失败次数过多，请 10 分钟后再试'})
   con=db();row=con.execute('SELECT * FROM users WHERE username=?',(str(req.get('username','')).strip(),)).fetchone();con.close();password=str(req.get('password',''))
   if not row or not hmac.compare_digest(row['password'],password_hash(password,row['salt'])):attempts.append(now);LOGIN_ATTEMPTS[ip]=attempts;time.sleep(.5);return self.out(401,{'error':'账号或密码错误'})
   LOGIN_ATTEMPTS.pop(ip,None)
   cookie,csrf=self.set_session(row['id']);return self.out(200,{'ok':True,'role':row['role'],'csrf':csrf},headers={'Set-Cookie':cookie})
  return self.out(404,{'error':'Not found'})
 def file(self,p,content_type=None):
  size=p.stat().st_size;start=0;end=size-1;status=200;raw=self.headers.get('Range','')
  if raw.startswith('bytes='):
   try:
    a,b=raw[6:].split('-',1);start=int(a) if a else 0;end=min(int(b),end) if b else end;status=206
   except Exception:start=0;end=size-1;status=200
  length=max(0,end-start+1);self.send_response(status);self.send_header('Content-Type',content_type or mimetypes.guess_type(p.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(length));self.send_header('Accept-Ranges','bytes');self.send_header('Cache-Control','private, max-age=86400')
  if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
  self.end_headers()
  with p.open('rb') as f:
   f.seek(start);left=length
   while left:
    chunk=f.read(min(1024*1024,left))
    if not chunk:break
    self.wfile.write(chunk);left-=len(chunk)
 def thumb(self,p):
  key=hashlib.sha1((str(p)+str(p.stat().st_mtime_ns)).encode()).hexdigest()+'.webp';dest=THUMBS/key
  if not dest.exists():
   from PIL import Image
   with Image.open(p) as im:
    im.thumbnail((480,480));im.convert('RGB').save(dest,'WEBP',quality=72,method=4)
  return self.file(dest,'image/webp')
 def video_thumb(self,p):
  key=hashlib.sha1((str(p)+str(p.stat().st_mtime_ns)).encode()).hexdigest()+'.jpg';dest=THUMBS/key
  if not dest.exists():subprocess.run([str(FFMPEG),'-y','-ss','0.5','-i',str(p),'-frames:v','1','-vf','scale=480:-2','-q:v','5',str(dest)],check=True,capture_output=True,timeout=30)
  return self.file(dest,'image/jpeg')
 def proxy(self,m):
  target=self.path.removeprefix("/api/comfy") or "/";data=self.body() if m=="POST" else None
  try:
   q=urllib.request.Request(COMFY+target,data=data,headers={"Content-Type":self.headers.get("Content-Type","application/json")},method=m)
   with urllib.request.urlopen(q,timeout=30) as r:self.out(r.status,r.read(),r.headers.get_content_type())
  except urllib.error.HTTPError as e:self.out(e.code,e.read(),e.headers.get_content_type())
  except Exception as e:self.out(503,{"error":"ComfyUI 未启动或无法连接","detail":str(e)})
 def library(self):
  root=user_input(self.current_user());return [{"name":p.name,"alias":p.stem,"kind":kind(p),"size":p.stat().st_size,"url":"/api/media/input/"+quote(p.name),"thumb":(("/api/thumb/input/" if kind(p)=='image' else "/api/vthumb/input/")+quote(p.name)) if kind(p) in {'image','video'} else None} for p in sorted(root.iterdir(),key=lambda x:x.stat().st_mtime,reverse=True) if p.is_file() and kind(p)]
 def results(self):
  user=self.current_user();root=result_root(user);found=[];meta=jobs()
  for p in root.rglob("*"):
   if user['role']=='admin' and p.is_relative_to(OUTPUT/'TheodoreUsers'):continue
   if p.is_file() and kind(p) in {"video","image"}:
    rel=p.relative_to(root).as_posix();full_rel=p.relative_to(OUTPUT).as_posix(); item={"name":p.name,"kind":kind(p),"path":rel,"size":p.stat().st_size,"mtime":p.stat().st_mtime,"url":"/api/media/output/"+quote(rel,safe='/'),"thumb":(("/api/thumb/output/" if kind(p)=='image' else "/api/vthumb/output/")+quote(rel,safe='/')) if kind(p) in {'image','video'} else None}
    match=next((v for k,v in meta.items() if full_rel.startswith(k) and (user['role']=='admin' or v.get('user_id')==user['id'])),None)
    if match:
     item['params']=match;gid,gname=group_info(match);item['group_id']=gid;item['group_name']=gname
    else:item['group_id']='ungrouped';item['group_name']='旧结果'
    found.append(item)
  return sorted(found,key=lambda x:x["mtime"],reverse=True)[:300]
 def do_GET(self):
  path=urlsplit(self.path).path
  if path=='/api/auth/me':
   user=self.current_user();con=db();bootstrap=con.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0;con.close();return self.out(200,{'authenticated':bool(user),'username':user['username'] if user else None,'role':user['role'] if user else None,'csrf':user['csrf'] if user else None,'needsBootstrap':bootstrap,'quota':usage(user['id']) if user and user['role']=='user' else None})
  if path in {'/','/login.html'} and not self.current_user():
   f=WEB/'login.html';return self.out(200,f.read_bytes(),'text/html; charset=utf-8')
  user=self.require_user()
  if not user:return
  if self.path.startswith('/api/comfy'):
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可访问 ComfyUI 代理'})
   return self.proxy('GET')
  if self.path=='/api/library':return self.out(200,self.library())
  if self.path=='/api/results':return self.out(200,self.results())
  if self.path=='/api/jobs':
   try:
    q=comfy_get('/queue');items=[]
    ahead=0
    for state,key in [('running','queue_running'),('pending','queue_pending')]:
     for row in q.get(key,[]):
      graph=row[2] if len(row)>2 else {};prefix='';title=''
      for node in graph.values():
       if node.get('class_type') in {'SaveVideo','SaveImageAdvanced'}:prefix=node.get('inputs',{}).get('filename_prefix','');break
      meta=jobs().get(prefix,{})
      own=meta.get('user_id')==user['id'];duration=float(meta.get('duration') or (1 if meta.get('kind')=='qwen-image-edit' else 5));items.append({'prompt_id':row[1],'state':state,'title':meta.get('title') or ('Qwen 图片编辑' if meta.get('kind') else '生成任务'),'mode':meta.get('mode') or meta.get('kind',''),'prefix':prefix,'own':own,'owner':meta.get('username','未知用户'),'ahead':ahead if state=='pending' else 0,'etaMinutes':round(ahead*6.5+duration/60,1) if state=='pending' else round(duration/60,1)});ahead+=1
    return self.out(200,items)
   except Exception as e:return self.out(503,{'error':str(e)})
  if self.path.startswith('/api/media/'):
   parts=self.path.split('/',4);root=user_input(user) if parts[3]=='input' else result_root(user);rel=urllib.parse.unquote(parts[4]);p=(root/rel).resolve()
   if root.resolve() not in p.parents or (parts[3]=='output' and user['role']=='admin' and p.is_relative_to(OUTPUT/'TheodoreUsers')) or not p.is_file():return self.out(404,{"error":"文件不存在"})
   return self.file(p)
  if self.path.startswith('/api/thumb/'):
   parts=self.path.split('/',4);root=user_input(user) if parts[3]=='input' else result_root(user);rel=urllib.parse.unquote(parts[4]);p=(root/rel).resolve()
   if root.resolve() not in p.parents or (parts[3]=='output' and user['role']=='admin' and p.is_relative_to(OUTPUT/'TheodoreUsers')) or not p.is_file() or kind(p)!='image':return self.out(404,{"error":"图片不存在"})
   try:return self.thumb(p)
   except Exception:return self.file(p)
  if self.path.startswith('/api/vthumb/'):
   parts=self.path.split('/',4);root=user_input(user) if parts[3]=='input' else result_root(user);rel=urllib.parse.unquote(parts[4]);p=(root/rel).resolve()
   if root.resolve() not in p.parents or (parts[3]=='output' and user['role']=='admin' and p.is_relative_to(OUTPUT/'TheodoreUsers')) or not p.is_file() or kind(p)!='video':return self.out(404,{"error":"视频不存在"})
   try:return self.video_thumb(p)
   except Exception:return self.out(404,{"error":"无法生成视频缩略图"})
  if self.path=='/api/admin/users':
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可查看账号'})
   con=db();rows=con.execute('SELECT id,username,role,created_at FROM users ORDER BY id').fetchall();con.close();return self.out(200,[dict(x,quota=usage(x['id']) if x['role']=='user' else None) for x in rows])
  f=(WEB/("index.html" if path=='/' else path.lstrip('/'))).resolve()
  if WEB not in f.parents or not f.is_file():return self.out(404,{"error":"Not found"})
  content=f.read_bytes()
  if f.name=='account.html':content=content.replace('所有公共账号共享公共素材库与结果库。'.encode(), '同一公共账号内共享素材和结果；不同公共账号之间相互隔离。'.encode())
  self.out(200,content,mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
 def do_POST(self):
  if self.path in {'/api/auth/register','/api/auth/login'}:return self.auth_post()
  user=self.require_user()
  if not user:return
  if not self.check_csrf(user):return self.out(403,{'error':'安全令牌无效，请刷新页面后重试'})
  if self.path=='/api/auth/logout':
   c=SimpleCookie(self.headers.get('Cookie',''));token=c.get('td_session')
   if token:
    con=db();con.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.value.encode()).hexdigest(),));con.commit();con.close()
   return self.out(200,{'ok':True},headers={'Set-Cookie':'td_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
  if self.path=='/api/admin/invites':
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可创建邀请码'})
   try:req=json.loads(self.body() or b'{}');days=max(1,min(30,int(req.get('days') or 7)));max_uses=max(1,min(100,int(req.get('maxUses') or 1)));code=secrets.token_urlsafe(9);con=db();con.execute('INSERT INTO invites VALUES(?,?,?,?,?,?)',(hashlib.sha256(code.encode()).hexdigest(),max_uses,0,int(time.time())+days*86400,1,user['id']));con.commit();con.close();return self.out(200,{'ok':True,'code':code,'days':days,'maxUses':max_uses})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/admin/create-public':
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可创建公共账号'})
   try:
    req=json.loads(self.body() or b'{}');username=str(req.get('username','')).strip();password=str(req.get('password',''))
    if len(username)<3 or len(password)<10:raise ValueError('账号至少 3 位，密码至少 10 位')
    salt=secrets.token_bytes(16);con=db();con.execute('INSERT INTO users(username,password,salt,role,created_at) VALUES(?,?,?,?,?)',(username,password_hash(password,salt),salt,'public',int(time.time())));con.commit();con.close();return self.out(200,{'ok':True})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path in {'/api/admin/reset-quota','/api/admin/reset-password'}:
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可管理账号'})
   try:
    req=json.loads(self.body() or b'{}');uid=int(req.get('userId'))
    if self.path.endswith('reset-quota'):
     con=db();con.execute('DELETE FROM daily_usage WHERE user_id=? AND day=?',(uid,time.strftime('%Y-%m-%d')));con.commit();con.close();return self.out(200,{'ok':True})
    password=str(req.get('password',''))
    if len(password)<10:raise ValueError('新密码至少 10 位')
    salt=secrets.token_bytes(16);con=db();con.execute('UPDATE users SET password=?,salt=? WHERE id=?',(password_hash(password,salt),salt,uid));con.execute('DELETE FROM sessions WHERE user_id=?',(uid,));con.commit();con.close();return self.out(200,{'ok':True})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path.startswith('/api/comfy'):
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可访问 ComfyUI 代理'})
   return self.proxy('POST')
  if self.path=='/api/generate':
   try:
    req=json.loads(self.body() or b'{}');count=1 if user['role']=='user' else max(1,min(8,int(req.get('count') or 1)));answers=[];all_jobs=jobs();ensure_quota(user,'video',count)
    for i in range(count):
     graph,prefix,duration=workflow(req,user);prefix=f'{prefix}_{i+1}' if count>1 else prefix;graph['92']['inputs']['filename_prefix']=prefix;answer=comfy_post('/prompt',{'prompt':graph});answers.append(answer.get('prompt_id'));all_jobs[prefix]=dict(req,duration=duration,user_id=user['id'],username=user['username'])
    consume(user,'video',count);save_jobs(all_jobs);return self.out(200,{'ok':True,'prompt_id':answers[0],'prompt_ids':answers,'count':count,'duration':duration})
   except urllib.error.HTTPError as e:
    try:detail=json.loads(e.read())
    except Exception:detail=str(e)
    return self.out(400,{'error':'工作流未通过 ComfyUI 校验','detail':detail})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/qwen-generate':
   try:
    req=json.loads(self.body() or b'{}');count=1 if user['role']=='user' else max(1,min(8,int(req.get('count') or 1)));answers=[];all_jobs=jobs();ensure_quota(user,'image',count)
    for i in range(count):
     graph,prefix=qwen_workflow(req,user);prefix=f'{prefix}_{i+1}' if count>1 else prefix;graph['195']['inputs']['filename_prefix']=prefix;answer=comfy_post('/prompt',{'prompt':graph});answers.append(answer.get('prompt_id'));all_jobs[prefix]=dict(req,kind='qwen-image-edit',user_id=user['id'],username=user['username'])
    consume(user,'image',count);save_jobs(all_jobs);return self.out(200,{'ok':True,'prompt_id':answers[0],'prompt_ids':answers,'count':count})
   except urllib.error.HTTPError as e:
    try:detail=json.loads(e.read())
    except Exception:detail=str(e)
    return self.out(400,{'error':'Qwen 工作流未通过校验','detail':detail})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/retry-result':
   try:
    req=json.loads(self.body() or b'{}');rel=req.get('path','');full_rel=rel if user['role']=='admin' else f"{output_prefix(user)}/{rel}";meta=next((v for k,v in jobs().items() if full_rel.startswith(k) and (user['role']=='admin' or v.get('user_id')==user['id'])),None)
    if not meta:raise ValueError('这个旧结果没有可用的生成参数')
    count=1 if user['role']=='user' else max(1,min(8,int(meta.get('count') or 1)));media_type='image' if meta.get('kind')=='qwen-image-edit' else 'video';ensure_quota(user,media_type,count);answers=[];all_jobs=jobs()
    for i in range(count):
     if meta.get('kind')=='qwen-image-edit':graph,prefix=qwen_workflow(meta,user);save_id='195'
     else:graph,prefix,duration=workflow(meta,user);save_id='92'
     prefix=f'{prefix}_{i+1}' if count>1 else prefix;graph[save_id]['inputs']['filename_prefix']=prefix;answer=comfy_post('/prompt',{'prompt':graph});answers.append(answer.get('prompt_id'));all_jobs[prefix]=meta
    consume(user,media_type,count);save_jobs(all_jobs);return self.out(200,{'ok':True,'prompt_ids':answers,'count':count})
   except urllib.error.HTTPError as e:
    try:detail=json.loads(e.read())
    except Exception:detail=str(e)
    return self.out(400,{'error':'重新生成未通过校验','detail':detail})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/import-result':
   try:
    req=json.loads(self.body() or b'{}');outroot=result_root(user);inroot=user_input(user);src=(outroot/req.get('path','')).resolve()
    if outroot.resolve() not in src.parents or (user['role']=='admin' and src.is_relative_to(OUTPUT/'TheodoreUsers')) or not src.is_file() or not kind(src):raise ValueError('结果文件不存在')
    dest=inroot/src.name
    if dest.exists() and dest.stat().st_size==src.stat().st_size:return self.out(200,{'ok':True,'name':dest.name,'existing':True})
    if dest.exists():dest=inroot/f'{src.stem}_{int(time.time())}{src.suffix}'
    dest.write_bytes(src.read_bytes());return self.out(200,{'ok':True,'name':dest.name})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/delete-result':
   try:
    req=json.loads(self.body() or b'{}');outroot=result_root(user);target=(outroot/req.get('path','')).resolve()
    if outroot.resolve() not in target.parents or (user['role']=='admin' and target.is_relative_to(OUTPUT/'TheodoreUsers')) or not target.is_file():raise ValueError('结果文件不存在')
    target.unlink();return self.out(200,{'ok':True})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/rename-asset':
   try:
    req=json.loads(self.body() or b'{}');inroot=user_input(user);src=(inroot/Path(req.get('name','')).name).resolve();new_name=Path(req.get('newName','')).name.strip()
    if inroot.resolve() not in src.parents or not src.is_file():raise ValueError('素材不存在')
    if not new_name:raise ValueError('请输入新名称')
    if not Path(new_name).suffix:new_name+=src.suffix
    if Path(new_name).suffix.lower()!=src.suffix.lower():raise ValueError('重命名不能修改文件格式')
    dest=(inroot/new_name).resolve()
    if inroot.resolve() not in dest.parents or dest.exists():raise ValueError('名称无效或已经存在')
    src.rename(dest);return self.out(200,{'ok':True,'name':dest.name})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/delete-asset':
   try:
    req=json.loads(self.body() or b'{}');inroot=user_input(user);target=(inroot/Path(req.get('name','')).name).resolve()
    if inroot.resolve() not in target.parents or not target.is_file():raise ValueError('素材不存在')
    target.unlink();return self.out(200,{'ok':True})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/cancel-job':
   try:
    req=json.loads(self.body() or b'{}');pid=req.get('prompt_id');state=req.get('state')
    if not pid:raise ValueError('任务不存在')
    queue=comfy_get('/queue');row=next((x for key in ('queue_running','queue_pending') for x in queue.get(key,[]) if len(x)>1 and x[1]==pid),None)
    if not row:raise ValueError('任务不存在')
    graph=row[2] if len(row)>2 else {};prefix=next((v.get('inputs',{}).get('filename_prefix','') for v in graph.values() if v.get('class_type') in {'SaveVideo','SaveImageAdvanced'}),'');meta=jobs().get(prefix,{})
    if state=='running':answer=comfy_post('/interrupt',{})
    else:answer=comfy_post('/queue',{'delete':[pid]})
    return self.out(200,{'ok':True,'result':answer})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/upload':
   msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+self.headers['Content-Type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+self.body());part=next((x for x in msg.iter_parts() if x.get_filename()),None)
   if not part:return self.out(400,{"error":"没有文件"})
   inroot=user_input(user);dest=inroot/Path(part.get_filename()).name
   if dest.exists():dest=inroot/f"{dest.stem}_{int(time.time())}{dest.suffix}"
   dest.write_bytes(part.get_payload(decode=True));return self.out(200,{"ok":True,"items":self.library()})
  if self.path=='/api/merge':
   req=json.loads(self.body() or b'{}');outroot=result_root(user);paths=[(outroot/x).resolve() for x in req.get('paths',[])]
   if len(paths)<2 or any(outroot.resolve() not in p.parents or not p.is_file() for p in paths):return self.out(400,{"error":"至少选择两个有效视频"})
   dest=outroot/('TheodoreMobile' if user['role']=='admin' else 'Merged');dest.mkdir(exist_ok=True);target=dest/f"merged_{int(time.time())}.mp4";lst=ROOT/f"concat_{user['id']}.txt";lst.write_text('\n'.join("file '"+str(p)+"'" for p in paths),encoding='utf-8')
   subprocess.run([str(FFMPEG),'-y','-f','concat','-safe','0','-i',str(lst),'-c','copy',str(target)],check=True,capture_output=True);lst.unlink(missing_ok=True);return self.out(200,{"ok":True,"path":target.relative_to(OUTPUT).as_posix()})
  self.out(404,{"error":"Not found"})
if __name__=='__main__':
 print('='*52);print('Theodore 独立手机导演台');print(f'电脑：http://127.0.0.1:{PORT}');print(f'手机：http://{lan_ip()}:{PORT}');print('ComfyUI 仍由秋叶启动器运行在 127.0.0.1:8188');print('='*52);ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
