from __future__ import annotations
import base64, hashlib, hmac, io, json, mimetypes, os, re, secrets, socket, sqlite3, subprocess, time, urllib.error, urllib.parse, urllib.request, uuid
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from threading import BoundedSemaphore

from director_v2 import DirectorV2API
from director_v2.catalog import serialize_media
from director_v2.legacy_history import legacy_history
from director_v2.database import connect as v2_connect, ensure_personal_workspace
from director_v2.semantic import pending_inputs as pending_embedding_inputs, save_vectors as save_embedding_vectors, search as semantic_rank
from director_v2.generation_contract import normalize_request
from director_v2.workflows import CHARACTER_MODELS, QWEN_MODELS, character_replace_graph, missing_dependencies, workflow_catalog

ROOT=Path(__file__).resolve().parent; WEB=ROOT/"web"; COMFY_ROOT=Path(os.environ.get('THEODORE_COMFY_ROOT',os.environ.get('COMFY_ROOT',ROOT.parent/'ComfyUI'))).resolve()
INPUT=Path(os.environ.get('THEODORE_INPUT_ROOT',str(COMFY_ROOT/"input"/"theodore_mobile"))); OUTPUT=Path(os.environ.get('THEODORE_OUTPUT_ROOT',str(COMFY_ROOT/"output"))); COMFY=os.environ.get('THEODORE_COMFY_URL',os.environ.get('COMFY_URL',"http://127.0.0.1:8188")).rstrip('/'); PORT=int(os.environ.get('THEODORE_PORT',os.environ.get('DIRECTOR_PORT','8190')))
API_DIR=Path(os.environ.get('THEODORE_WORKFLOW_DIR',os.environ.get('WORKFLOW_DIR',ROOT.parent/'workflows'))).resolve(); JOBS=Path(os.environ.get('THEODORE_JOBS_FILE',str(ROOT/"jobs.json"))); AUTH_DB=Path(os.environ.get('THEODORE_AUTH_DB',str(ROOT/"auth.db"))); THUMBS=Path(os.environ.get('THEODORE_THUMBS',str(ROOT/".thumbs"))); FFMPEG=Path(os.environ.get('THEODORE_FFMPEG',os.environ.get('FFMPEG_PATH','ffmpeg')))
H3_R2V_UNET='minimax_h3_ref2va_pruned_int8_convrot.safetensors'; H3_T2V_UNET='minimax_h3_fl2va_int8_convrot.safetensors'; H3_CLIP='qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
INPUT.mkdir(parents=True,exist_ok=True)
THUMBS.mkdir(exist_ok=True)
V2_API=DirectorV2API(AUTH_DB)
CHARACTER_WORKFLOW=Path(os.environ.get('THEODORE_CHARACTER_WORKFLOW',str(API_DIR/'minimax_character_replace_v4_api.json')))
LOGIN_ATTEMPTS={}
THUMB_WORKERS=BoundedSemaphore(2)
def db():
 con=sqlite3.connect(AUTH_DB,timeout=10);con.row_factory=sqlite3.Row
 con.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE COLLATE NOCASE,password BLOB NOT NULL,salt BLOB NOT NULL,role TEXT NOT NULL DEFAULT 'user',created_at INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
 CREATE TABLE IF NOT EXISTS invites(code_hash TEXT PRIMARY KEY,max_uses INTEGER NOT NULL DEFAULT 1,uses INTEGER NOT NULL DEFAULT 0,expires_at INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_by INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS daily_usage(user_id INTEGER NOT NULL,day TEXT NOT NULL,videos INTEGER NOT NULL DEFAULT 0,images INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,day));
 CREATE TABLE IF NOT EXISTS user_ai_configs(user_id INTEGER PRIMARY KEY,base_url TEXT NOT NULL,api_key TEXT NOT NULL,text_model TEXT NOT NULL,image_model TEXT NOT NULL,image_size TEXT NOT NULL DEFAULT '1536x1024',updated_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));
 CREATE TABLE IF NOT EXISTS asset_projects(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,source_type TEXT NOT NULL,source_text TEXT NOT NULL,characters_json TEXT NOT NULL,storyboard_json TEXT NOT NULL,assets_json TEXT NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id));''')
 cols={row[1] for row in con.execute('PRAGMA table_info(user_ai_configs)').fetchall()}
 if 'classification_model' not in cols:con.execute("ALTER TABLE user_ai_configs ADD COLUMN classification_model TEXT NOT NULL DEFAULT ''")
 if 'embedding_model' not in cols:con.execute("ALTER TABLE user_ai_configs ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''")
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
def clean_filename(value,fallback='asset'):
 value=re.sub(r'[\\/:*?"<>|\x00-\x1f]+','_',str(value)).strip(' ._')[:52]
 return value or fallback
def safe_login_target(value):
 # This value is inserted in an inline script, so accept known local pages only.
 return value if value in {'/studio.html','/assets.html','/account.html','/settings.html','/discover.html'} else '/studio.html'
def ai_config(user_id):
 con=db();row=con.execute('SELECT base_url,api_key,text_model,image_model,image_size,classification_model,embedding_model,updated_at FROM user_ai_configs WHERE user_id=?',(user_id,)).fetchone();con.close();return dict(row) if row else None
def ai_endpoint(base_url,path):return base_url.rstrip('/')+'/'+path.lstrip('/')
def ai_request(config,path,payload,timeout=180):
 if not config or not config.get('api_key'):raise ValueError('请先在「我的 API」中完成配置')
 req=urllib.request.Request(ai_endpoint(config['base_url'],path),data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers={'Content-Type':'application/json','Authorization':'Bearer '+config['api_key']},method='POST')
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read())
 except urllib.error.HTTPError as e:
  # Providers may echo request headers or private configuration in their body.
  raise ValueError(f'模型 API 返回 HTTP {e.code}，请检查模型名称、额度和接口配置') from None
 except urllib.error.URLError as e:raise ValueError(f'无法连接模型 API：{e.reason}')
def extract_json(text):
 text=str(text or '').strip();text=re.sub(r'^```(?:json)?\s*|\s*```$','',text,flags=re.I)
 try:return json.loads(text)
 except Exception:
  start=text.find('{');end=text.rfind('}')
  if start>=0 and end>start:return json.loads(text[start:end+1])
  raise ValueError('文本模型没有返回可解析的 JSON')
def classification_image_data(path,media_kind):
 source=path
 if media_kind=='video':
  key=hashlib.sha1((str(path)+str(path.stat().st_mtime_ns)+'classifier').encode()).hexdigest()+'.jpg';source=THUMBS/key
  if not source.exists():subprocess.run([str(FFMPEG),'-y','-ss','1','-i',str(path),'-frames:v','1','-vf','scale=1280:-2','-q:v','4',str(source)],check=True,capture_output=True,timeout=45)
 if media_kind not in {'image','video'}:return None
 from PIL import Image
 with Image.open(source) as image:
  image.thumbnail((1600,1600));image=image.convert('RGB');buffer=io.BytesIO();image.save(buffer,'JPEG',quality=86,optimize=True)
 return 'data:image/jpeg;base64,'+base64.b64encode(buffer.getvalue()).decode()
def run_v2_classification(req,user):
 media_id=str(req.get('mediaId','')).strip()
 if not media_id:raise ValueError('请选择要分类的素材')
 context=V2_API.classification_context(user,media_id,input_root=user_input(user),output_root=result_root(user));media=context['media'];cfg=ai_config(user['id'])
 if not cfg or not cfg.get('api_key'):
  status,result=V2_API.post('/api/v2/classifications/draft',user,{'mediaId':media_id,'provider':'manual','classification':{'assetType':'other','summary':'','retrievalDescriptor':{'displayLabel':media['name'],'searchCaption':'','bestFor':[],'avoidFor':[],'usageNotes':'','keywords':[]},'links':[],'proposedEntities':[]}},input_root=user_input(user),output_root=result_root(user))
  if status>=400:raise ValueError(result.get('error','无法创建手工分类草稿'))
  return dict(result,warning='未配置多模态接口，已打开可编辑草稿，素材仍可正常使用')
 vocabulary=[{'id':x['id'],'type':x['type'],'name':x['name']} for x in context['entities'][:200]]
 schema='''只返回合法 JSON 对象，结构为：{"assetType":"character|character_look|location|scene_variant|prop|shot_result|other","summary":"简洁客观描述","attributes":{},"continuity":{},"retrievalDescriptor":{"displayLabel":"简短标签","searchCaption":"供 AI 检索的完整描述","bestFor":["适用情况"],"avoidFor":["不适用情况"],"usageNotes":"使用和连续性注意事项","keywords":["关键词"]},"links":[{"targetType":"仅可使用已有实体 type","targetId":"已有实体 id","role":"identity_reference|look_reference|environment_reference|shot_result|theme_reference"}],"proposedEntities":[{"type":"character|character_look|location|scene_variant|theme","name":"建议名称","metadata":{}}],"confidence":0.0}。没有把握时不要关联已有实体，可放入 proposedEntities。不要输出 Markdown。'''
 prompt=f'''你是短剧导演台的多模态素材归档助手。分析素材本身，不要求用户预先整理。素材名：{media['name']}；媒体类型：{media['kind']}。已有可复用实体如下：{json.dumps(vocabulary,ensure_ascii=False)}。{schema}'''
 visual=classification_image_data(context['path'],media['kind']);content=prompt
 if visual:content=[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':visual,'detail':'low'}}]
 classifier_model=cfg.get('classification_model') or cfg['text_model'];answer=ai_request(cfg,'chat/completions',{'model':classifier_model,'messages':[{'role':'system','content':'准确分析影视素材，并输出严格 JSON。不要臆造看不见的信息。'},{'role':'user','content':content}],'temperature':0.15},timeout=180)
 try:classification=extract_json(answer['choices'][0]['message']['content'])
 except (KeyError,IndexError,TypeError):raise ValueError('多模态模型响应中缺少 choices[0].message.content')
 if not isinstance(classification,dict):raise ValueError('多模态模型没有返回分类对象')
 descriptor=classification.setdefault('retrievalDescriptor',{})
 if not isinstance(descriptor,dict):classification['retrievalDescriptor']={};descriptor=classification['retrievalDescriptor']
 descriptor.setdefault('displayLabel',media['name']);descriptor.setdefault('searchCaption',classification.get('summary',''))
 for key in ('bestFor','avoidFor','keywords'):
  if not isinstance(descriptor.get(key),list):descriptor[key]=[]
 descriptor.setdefault('usageNotes','');classification.setdefault('links',[]);classification.setdefault('proposedEntities',[])
 status,result=V2_API.post('/api/v2/classifications/draft',user,{'mediaId':media_id,'provider':'openai-compatible','model':classifier_model,'classification':classification},input_root=user_input(user),output_root=result_root(user))
 if status>=400:raise ValueError(result.get('error','无法保存分类草稿'))
 return result
def analyze_v2_project(req,user):
 project_id=str(req.get('projectId','')).strip();source=str(req.get('sourceText','')).strip()
 if not project_id:raise ValueError('请先创建或选择项目')
 if len(source)<20:raise ValueError('请输入至少 20 个字的剧本、小说或故事梗概')
 if len(source)>200000:raise ValueError('当前单次最多处理 20 万字，请拆分章节')
 visual_style=str(req.get('visualStyle') or '电影感写实').strip();aspect=str(req.get('aspect') or '16:9').strip();target=max(1,min(300,int(req.get('shotCount') or 12)))
 save_status,save_result=V2_API.post('/api/v2/projects/source',user,{'projectId':project_id,'sourceType':req.get('sourceType','auto'),'sourceText':source,'visualStyle':visual_style,'settings':{'aspect':aspect,'shotCount':target}},input_root=user_input(user),output_root=result_root(user))
 if save_status>=400:raise ValueError(save_result.get('error','无法保存剧本'))
 cfg=ai_config(user['id'])
 if not cfg or not cfg.get('api_key'):raise ValueError('剧本已保存；请先在「我的 API」中完成文本模型配置后再拆解')
 system='''你是短剧项目的制片拆解师和分镜导演。用户可能只提供杂乱文本，不得要求其先结构化。提取可跨项目复用的人物与地点，并生成完整、有叙事连续性的可执行分镜。只输出合法 JSON，不要 Markdown。所有描述与提示词使用中文。'''
 schema='''返回结构：{"projectTitle":"","logline":"","visualBible":"","aspect":"16:9","characters":[{"name":"","description":"年龄、外貌、性格","continuity":"不可变化特征","visualPrompt":"人物设定图提示词"}],"scenes":[{"id":"scene_1","name":"地点/场景名","description":"空间、陈设、光线","time":"","weather":"","visualPrompt":"无人场景参考图提示词"}],"shots":[{"title":"","sceneId":"scene_1","characters":["人物名"],"duration":3,"shotSize":"","angle":"","lens":"","camera":"运镜","action":"","dialogue":"","audio":"","prompt":"可直接用于视频生成的完整镜头提示词","negativePrompt":""}]}'''
 prompt=f'''目标镜头数约 {target}；画幅 {aspect}；视觉风格：{visual_style}。可以根据叙事需要调整镜头数，但要覆盖完整情节。\n\n原文：\n{source}\n\n{schema}'''
 answer=ai_request(cfg,'chat/completions',{'model':cfg['text_model'],'messages':[{'role':'system','content':system},{'role':'user','content':prompt}],'temperature':0.3},timeout=240)
 try:data=extract_json(answer['choices'][0]['message']['content'])
 except (KeyError,IndexError,TypeError):raise ValueError('文本模型响应中缺少有效内容')
 if not isinstance(data,dict) or not isinstance(data.get('shots'),list) or not data['shots']:raise ValueError('模型没有返回有效分镜')
 data['aspect']=aspect
 for index,shot in enumerate(data['shots'],1):shot['number']=index;shot['duration']=max(.5,min(60,float(shot.get('duration') or 3)))
 status,result=V2_API.post('/api/v2/projects/apply-analysis',user,{'projectId':project_id,'analysis':data},input_root=user_input(user),output_root=result_root(user))
 if status>=400:raise ValueError(result.get('error','无法保存拆解结果'))
 detail_status,detail=V2_API.get('/api/v2/projects/'+project_id,user)
 if detail_status>=400:raise ValueError(detail.get('error','无法读取项目'))
 return detail
def v2_reference_name(media_id,user):
 context=V2_API.classification_context(user,media_id,input_root=user_input(user),output_root=result_root(user));path=context['path'];inroot=user_input(user).resolve()
 if path.parent==inroot:return path.name
 suffix=path.suffix.lower();dest=inroot/f'_ref_{media_id}{suffix}'
 if dest.is_symlink() or (dest.exists() and not os.path.samefile(path,dest)):
  raise ValueError('生成引用别名与原素材不一致，请检查内部引用文件；系统不会覆盖现有文件')
 if not dest.exists():
  try:os.link(path,dest)
  except OSError as e:raise ValueError(f'无法建立零复制生成引用：{e}')
 return dest.name
def submit_v2_generation(req,user):
 cfg=ai_config(user['id']) or {};req,parameters=normalize_request(req,cfg.get('image_size') or '1536x1024')
 media_kind=req['kind'];count=req['count'];prompt=req['prompt']
 if media_kind=='image' and not cfg.get('api_key'):raise ValueError('请先在「我的 API」中完成图片模型配置')
 project_id=str(req.get('projectId') or '') or None;shot_id=str(req.get('shotId') or '') or None;parent_version_id=str(req.get('parentVersionId') or '') or None
 media_ids=req['mediaIds'];reference_kinds=[];workflow_id=parameters.get('workflowId','')
 for media_id in media_ids:
  context=V2_API.classification_context(user,media_id,input_root=user_input(user),output_root=result_root(user));reference_kinds.append(context['media']['kind'])
 if media_kind=='image_edit' and any(kind!='image' for kind in reference_kinds):raise ValueError('图片编辑只能选择参考图片')
 if media_kind=='video' and req.get('mode')=='i2v' and any(kind!='image' for kind in reference_kinds):raise ValueError('首尾帧必须是图片')
 if media_kind=='video' and req.get('mode')=='r2v' and (any(kind not in {'image','video'} for kind in reference_kinds) or reference_kinds.count('image')>9 or reference_kinds.count('video')>3):raise ValueError('参考生成最多使用 9 张图片和 3 个视频')
 if workflow_id=='character_replace' and reference_kinds!=['video','image']:raise ValueError('人物替换的源素材必须是视频，人物参考必须是图片')
 if workflow_id in {'character_replace','qwen_image_edit'}:
  missing=missing_dependencies(CHARACTER_WORKFLOW if workflow_id=='character_replace' else API_DIR/'qwen_image_edit_2511_api.json',COMFY_ROOT,CHARACTER_MODELS if workflow_id=='character_replace' else QWEN_MODELS)
  if missing:raise ValueError('工作流依赖未就绪：'+'、'.join(missing))
 if req.get('entityId'):
  connection=v2_connect(AUTH_DB)
  try:workspace=ensure_personal_workspace(connection,user);V2_API._validate_target(connection,workspace['id'],req.get('entityType',''),req['entityId'])
  finally:connection.close()
 ensure_quota(user,'video' if media_kind=='video' else 'image',count)
 status,task=V2_API.post('/api/v2/tasks/create',user,{'kind':media_kind,'title':req.get('title') or ('人物替换' if workflow_id=='character_replace' else '图片生成' if media_kind=='image' else 'Qwen 图片编辑' if media_kind=='image_edit' else '视频生成'),'prompt':prompt,'parameters':parameters,'inputMedia':media_ids,'count':count,'projectId':project_id,'shotId':shot_id,'provider':'openai-compatible' if media_kind=='image' else 'comfyui','model':cfg.get('image_model','') if media_kind=='image' else workflow_id or 'local-workflow'},input_root=user_input(user),output_root=result_root(user))
 if status>=400:raise ValueError(task.get('error','无法创建任务'))
 task_id=task['id']
 try:
  ref_names=[v2_reference_name(x,user) for x in media_ids]
  if media_kind=='image':
   ensure_quota(user,'image',count);payload={'model':cfg.get('image_model',''),'prompt':prompt,'n':count,'size':str(req.get('size') or cfg.get('image_size') or '1536x1024'),'response_format':'b64_json'}
   answer=ai_request(cfg,'images/generations',payload,timeout=300);items=answer.get('data') or []
   if not items:raise ValueError('图片模型没有返回结果')
   task_root=result_root(user)/'TheodoreV2'/task_id;task_root.mkdir(parents=True,exist_ok=True);paths=[]
   for index,item in enumerate(items[:count],1):
    raw=None;ext='.png'
    if item.get('b64_json'):raw=base64.b64decode(item['b64_json'],validate=True)
    elif item.get('url'):
     with urllib.request.urlopen(item['url'],timeout=120) as response:raw=response.read(30*1024*1024+1);ext={'image/jpeg':'.jpg','image/webp':'.webp','image/png':'.png'}.get(response.headers.get_content_type(),'.png')
    if not raw or len(raw)>30*1024*1024:continue
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as generated:
     ext={'PNG':'.png','JPEG':'.jpg','WEBP':'.webp'}.get(generated.format)
     if not ext:raise ValueError('图片模型返回了不支持的图片格式')
     generated.verify()
    path=task_root/f'image_{index:02d}{ext}';path.write_bytes(raw);paths.append(path)
   if not paths:raise ValueError('图片模型没有返回可保存的图片')
   consume(user,'image',len(paths));V2_API.post('/api/v2/catalog/sync',user,{},input_root=user_input(user),output_root=result_root(user));versions=[]
   from director_v2.catalog import deterministic_media_id
   for path in paths:
    media_id=deterministic_media_id(user['id'],'output',path.relative_to(result_root(user)).as_posix());version_status,version=V2_API.post('/api/v2/versions/register',user,{'taskId':task_id,'mediaId':media_id,'parentVersionId':parent_version_id},input_root=user_input(user),output_root=result_root(user))
    if version_status>=400:raise ValueError(version.get('error','无法保存生成版本'))
    versions.append(dict(version,mediaId=media_id,url='/api/media/output/'+quote(path.relative_to(result_root(user)).as_posix(),safe='/')))
   if len(versions)<count:
    error=f'请求 {count} 个结果，模型仅返回 {len(versions)} 个；已保留成功结果'
    V2_API.post('/api/v2/tasks/mark',user,{'taskId':task_id,'status':'failed','error':error},input_root=user_input(user),output_root=result_root(user))
    return {'taskId':task_id,'status':'failed','versions':versions,'count':len(versions),'error':error}
   return {'taskId':task_id,'status':'completed','versions':versions,'count':len(versions)}
  if media_kind not in {'video','image_edit'}:raise ValueError('不支持的生成类型')
  ensure_quota(user,'image' if media_kind=='image_edit' else 'video',count);job_ids=[];prefixes=[];legacy=jobs()
  for index in range(count):
   work=dict(req,selected=ref_names,firstFrame=v2_reference_name(req['firstFrameId'],user) if req.get('firstFrameId') else '',lastFrame=v2_reference_name(req['lastFrameId'],user) if req.get('lastFrameId') else '')
   if req.get('seed') not in (None,''):work['seed']=int(req['seed'])+index
   if workflow_id=='character_replace':
    graph=character_replace_graph(CHARACTER_WORKFLOW,source_video=input_prefix(user)+'/'+ref_names[0],character_image=input_prefix(user)+'/'+ref_names[1],prompt=prompt,seed=work['seed'],aspect=req['aspect'],resolution=req['resolution'],duration=req['duration'],start_time=req['startTime'],preserve_audio=req['preserveAudio']);prefix='';duration=req['duration']
   elif media_kind=='image_edit':graph,prefix=qwen_workflow(work,user);duration=None
   else:graph,prefix,duration=workflow(work,user)
   if index==0 and workflow_id in {'character_replace','qwen_image_edit'}:validate_local_graph_nodes(graph)
   prefix=f'{output_prefix(user)}/TheodoreV2/{task_id}/result_{index+1:02d}'
   save_node='195' if media_kind=='image_edit' else '92';graph[save_node]['inputs']['filename_prefix']=prefix
   answer=comfy_post('/prompt',{'prompt':graph});job_id=answer.get('prompt_id')
   if not job_id:raise ValueError('ComfyUI 没有接受任务，未返回任务 ID')
   job_ids.append(job_id);prefixes.append(prefix);legacy[prefix]=dict(req,user_id=user['id'],username=user['username'],count=1,v2_task_id=task_id,parent_version_id=parent_version_id)
   save_jobs(legacy)
   saved_status,saved=V2_API.post('/api/v2/tasks/mark',user,{'taskId':task_id,'status':'queued','jobIds':job_ids,'parametersPatch':{'prefixes':prefixes,'parentVersionId':parent_version_id}},input_root=user_input(user),output_root=result_root(user))
   if saved_status>=400:raise ValueError(saved.get('error','无法记录已提交的任务'))
  save_jobs(legacy);consume(user,'image' if media_kind=='image_edit' else 'video',count)
  mark_status,marked=V2_API.post('/api/v2/tasks/mark',user,{'taskId':task_id,'status':'queued','jobIds':job_ids,'parametersPatch':{'prefixes':prefixes,'parentVersionId':parent_version_id}},input_root=user_input(user),output_root=result_root(user))
  if mark_status>=400:raise ValueError(marked.get('error','无法保存任务状态'))
  return {'taskId':task_id,'status':'queued','jobIds':job_ids,'count':count}
 except Exception as error:
  V2_API.post('/api/v2/tasks/mark',user,{'taskId':task_id,'status':'failed','error':str(error)},input_root=user_input(user),output_root=result_root(user));raise
def reconcile_v2_tasks(user):
 status,payload=V2_API.get('/api/v2/tasks',user)
 if status>=400:return
 active=[task for task in payload.get('tasks',[]) if task['status'] in {'queued','running'} or (task['status']=='failed' and task.get('jobIds'))]
 if not active:return
 running_ids=set();queued_ids=set()
 queue_available=False
 try:
  queue=comfy_get('/queue');running_ids={row[1] for row in queue.get('queue_running',[]) if len(row)>1};queued_ids={row[1] for row in queue.get('queue_pending',[]) if len(row)>1};queue_available=True
 except Exception:pass
 from director_v2.catalog import deterministic_media_id
 catalog_synced=False
 for task in active:
  detail_status,detail=V2_API.get('/api/v2/tasks/'+task['id'],user)
  if detail_status>=400:continue
  found=[]
  for prefix in detail.get('parameters',{}).get('prefixes',[]):
   relative=str(prefix);base=output_prefix(user).rstrip('/')+'/'
   if user['role']!='admin':
    if not relative.startswith(base):continue
    relative=relative[len(base):]
   rel_path=Path(relative);folder=(result_root(user)/rel_path.parent).resolve();root=result_root(user).resolve()
   if root not in folder.parents and folder!=root:continue
   if folder.is_dir():found.extend(path for path in folder.glob(rel_path.name+'*') if path.is_file() and kind(path)==('video' if task['kind']=='video' else 'image'))
  histories=[]
  if queue_available and task.get('jobIds'):
   try:histories=[comfy_get('/history/'+quote(job_id,safe='')).get(job_id,{}) for job_id in task['jobIds']]
   except Exception:histories=[]
  failed_history=next((history for history in histories if history.get('status',{}).get('status_str')=='error'),None)
  if failed_history:
   details=[]
   for entry in failed_history.get('status',{}).get('messages',[]):
    if isinstance(entry,list) and len(entry)>1 and isinstance(entry[1],dict):
     details.append(str(entry[1].get('exception_message') or entry[1].get('message') or ''))
   reason='；'.join(x for x in details if x)
   message='生成失败（可能是显存不足）'+(('：'+reason) if reason else '')
   V2_API.post('/api/v2/tasks/mark',user,{'taskId':task['id'],'status':'failed','error':message},input_root=user_input(user),output_root=result_root(user))
   continue
  if found and not catalog_synced:
   sync_status,_=V2_API.post('/api/v2/catalog/sync',user,{},input_root=user_input(user),output_root=result_root(user))
   catalog_synced=sync_status<400
  if found and not catalog_synced:continue
  for path in sorted(set(found)):
   rel=path.relative_to(result_root(user)).as_posix();media_id=deterministic_media_id(user['id'],'output',rel)
   V2_API.post('/api/v2/versions/register',user,{'taskId':task['id'],'mediaId':media_id,'parentVersionId':detail.get('parameters',{}).get('parentVersionId')},input_root=user_input(user),output_root=result_root(user))
  latest_status,latest=V2_API.get('/api/v2/tasks/'+task['id'],user)
  if latest_status>=400 or latest['status']=='completed':continue
  job_ids=set(task.get('jobIds') or [])
  if job_ids & (running_ids|queued_ids):
   next_status='running' if job_ids & running_ids else 'queued'
   if latest['status'] in {'queued','running'} and next_status!=latest['status']:V2_API.post('/api/v2/tasks/mark',user,{'taskId':task['id'],'status':next_status},input_root=user_input(user),output_root=result_root(user))
  elif queue_available and job_ids and latest['status']!='failed':
   finished=bool(histories) and all(history.get('status',{}).get('completed') for history in histories)
   if finished:
    message='工作流已结束但结果数量不足；已保留可用结果'
    V2_API.post('/api/v2/tasks/mark',user,{'taskId':task['id'],'status':'failed','error':message},input_root=user_input(user),output_root=result_root(user))
def cancel_v2_task(req,user):
 task_id=str(req.get('taskId',''));status,task=V2_API.get('/api/v2/tasks/'+task_id,user)
 if status>=400:raise ValueError(task.get('error','任务不存在'))
 cancelled=[]
 try:
  queue=comfy_get('/queue')
  for state,key in [('running','queue_running'),('pending','queue_pending')]:
   for row in queue.get(key,[]):
    if row[1] not in task.get('jobIds',[]):continue
    if state=='running':comfy_post('/interrupt',{})
    else:comfy_post('/queue',{'delete':[row[1]]})
    cancelled.append(row[1])
 except Exception as error:
  if task.get('jobIds'):raise ValueError(f'无法取消 ComfyUI 任务：{error}')
 status,result=V2_API.post('/api/v2/tasks/mark',user,{'taskId':task_id,'status':'cancelled','jobIds':task.get('jobIds',[])},input_root=user_input(user),output_root=result_root(user))
 if status>=400:raise ValueError(result.get('error','无法更新任务'))
 return {'taskId':task_id,'status':'cancelled','cancelledJobIds':cancelled}

def fail_stalled_v2_task(req,user):
 task_id=str(req.get('taskId',''));status,task=V2_API.get('/api/v2/tasks/'+task_id,user)
 if status>=400:raise ValueError(task.get('error','任务不存在'))
 if task.get('status') not in {'queued','running'}:raise ValueError('只有排队中或运行中的任务可以标记为异常')
 stopped=[];cleanup_error=''
 try:
  queue=comfy_get('/queue')
  for state,key in [('running','queue_running'),('pending','queue_pending')]:
   for row in queue.get(key,[]):
    if len(row)<2 or row[1] not in task.get('jobIds',[]):continue
    if state=='running':comfy_post('/interrupt',{})
    else:comfy_post('/queue',{'delete':[row[1]]})
    stopped.append(row[1])
 except Exception as error:cleanup_error=str(error)
 message='用户确认任务异常并结束，可调整参数后创建新任务'
 if cleanup_error:message+='；旧队列清理未确认：'+cleanup_error
 status,result=V2_API.post('/api/v2/tasks/mark',user,{
  'taskId':task_id,'status':'failed','jobIds':task.get('jobIds',[]),'error':message,
 },input_root=user_input(user),output_root=result_root(user))
 if status>=400:raise ValueError(result.get('error','无法更新任务'))
 return {'taskId':task_id,'status':'failed','stoppedJobIds':stopped,'warning':cleanup_error}
def run_v2_semantic_search(req,user):
 query=str(req.get('query','')).strip();limit=max(1,min(200,int(req.get('limit') or 60)))
 if not query:raise ValueError('请输入语义检索内容')
 cfg=ai_config(user['id'])
 if not cfg or not cfg.get('api_key') or not cfg.get('embedding_model'):
  status,payload=V2_API.get('/api/v2/media',user,'q='+urllib.parse.quote(query)+'&limit='+str(limit))
  if status>=400:raise ValueError(payload.get('error','检索失败'))
  return dict(payload,method='keyword_fallback',warning='未配置嵌入模型，已使用关键词检索')
 model=cfg['embedding_model'];connection=v2_connect(AUTH_DB);workspace=ensure_personal_workspace(connection,user)
 try:
  pending=pending_embedding_inputs(connection,workspace['id'],model,provider=cfg.get('base_url','configured-provider'))
  for offset in range(0,len(pending),64):
   batch=pending[offset:offset+64];answer=ai_request(cfg,'embeddings',{'model':model,'input':[item['text'] for item in batch]},timeout=120);data=sorted(answer.get('data') or [],key=lambda item:item.get('index',0));vectors=[item.get('embedding') for item in data]
   save_embedding_vectors(connection,cfg.get('base_url','configured-provider'),model,batch,vectors)
  answer=ai_request(cfg,'embeddings',{'model':model,'input':[query]},timeout=60);query_vector=(answer.get('data') or [{}])[0].get('embedding')
  if not query_vector:raise ValueError('嵌入模型没有返回查询向量')
  ranked=semantic_rank(connection,workspace['id'],model,query_vector,limit,provider=cfg.get('base_url','configured-provider'))
  return {'items':[dict(serialize_media(row),semanticScore=round(score,6)) for row,score in ranked],'total':len(ranked),'offset':0,'limit':limit,'hasMore':False,'method':'semantic','model':model,'embedded':len(pending)}
 finally:connection.close()
def project_row(row):
 d=dict(row)
 for key in ('characters_json','storyboard_json','assets_json'):
  d[key.removesuffix('_json')]=json.loads(d.pop(key) or '[]')
 return d
def list_asset_projects(user_id):
 con=db();rows=con.execute('SELECT * FROM asset_projects WHERE user_id=? ORDER BY updated_at DESC',(user_id,)).fetchall();con.close();return [project_row(x) for x in rows]
def save_asset_project(data):
 con=db();con.execute('''INSERT INTO asset_projects(id,user_id,name,source_type,source_text,characters_json,storyboard_json,assets_json,status,error,created_at,updated_at)
 VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_type=excluded.source_type,source_text=excluded.source_text,characters_json=excluded.characters_json,storyboard_json=excluded.storyboard_json,assets_json=excluded.assets_json,status=excluded.status,error=excluded.error,updated_at=excluded.updated_at''',(data['id'],data['user_id'],data['name'],data['source_type'],data['source_text'],json.dumps(data.get('characters',[]),ensure_ascii=False),json.dumps(data.get('storyboard',{}),ensure_ascii=False),json.dumps(data.get('assets',[]),ensure_ascii=False),data.get('status','ready'),data.get('error',''),data.get('created_at',int(time.time())),int(time.time())));con.commit();con.close()
def analyze_asset_project(req,user):
 source=str(req.get('sourceText','')).strip()
 if len(source)<20:raise ValueError('请输入至少 20 个字的剧本或小说内容')
 if len(source)>60000:raise ValueError('当前单次最多处理 60000 字，请先拆分章节')
 characters=req.get('characters') or []
 known=[]
 for i,c in enumerate(characters[:20]):
  name=str(c.get('name','')).strip()
  if name:known.append({'id':str(c.get('id') or f'char_{i+1}'),'name':name,'description':str(c.get('description','')).strip(),'referenceFile':Path(str(c.get('referenceFile',''))).name})
 cfg=ai_config(user['id']);visual_style=str(req.get('visualStyle','电影感写实')).strip();aspect=str(req.get('aspect','16:9')).strip();shot_target=max(3,min(80,int(req.get('shotCount') or 12)))
 system='''你是一位影视资产总监和分镜导演。把用户提供的剧本或小说转成可执行的制作资产。只输出一个合法 JSON 对象，不要 Markdown。所有描述和提示词使用中文。镜头必须覆盖故事的完整叙事顺序，并保持人物、服装、场景连续。'''
 schema='''JSON 结构必须是：{"projectTitle":"","logline":"","visualBible":"","characters":[{"id":"char_1","name":"","description":"外貌、年龄、服装、性格与连续性锚点","referenceFile":"保留输入值或空字符串","visualPrompt":"用于生成干净角色设定图的完整提示词"}],"scenes":[{"id":"scene_1","name":"","description":"空间、时间、光线、陈设与连续性锚点","visualPrompt":"用于生成无人场景参考图的完整提示词"}],"shots":[{"number":1,"title":"","sceneId":"scene_1","characters":["角色名"],"duration":3,"shotSize":"","camera":"","action":"","dialogue":"","audio":"","prompt":"可直接用于视频模型的完整镜头提示词"}]}'''
 prompt=f'''素材类型：{str(req.get('sourceType','auto'))}\n目标镜头数：约 {shot_target}\n画幅：{aspect}\n整体视觉风格：{visual_style}\n已知人物设定（必须优先采用并补全，不得改变 referenceFile）：\n{json.dumps(known,ensure_ascii=False)}\n\n原文：\n{source}\n\n{schema}'''
 answer=ai_request(cfg,'chat/completions',{'model':cfg['text_model'],'messages':[{'role':'system','content':system},{'role':'user','content':prompt}],'temperature':0.35})
 try:content=answer['choices'][0]['message']['content']
 except Exception:raise ValueError('文本模型响应中缺少 choices[0].message.content')
 data=extract_json(content);shots=data.get('shots') or []
 if not shots:raise ValueError('模型没有生成分镜')
 for i,s in enumerate(shots,1):s['number']=i;s['duration']=max(.5,min(15,float(s.get('duration') or 3)))
 generated_characters=data.get('characters') or known;known_by_name={x['name']:x for x in known}
 for i,c in enumerate(generated_characters,1):
  original=known_by_name.get(str(c.get('name','')).strip(),{});c['id']=str(c.get('id') or original.get('id') or f'char_{i}');c['referenceFile']=original.get('referenceFile') or Path(str(c.get('referenceFile',''))).name
 requested_id=str(req.get('id') or '');old_assets=[];created_at=int(time.time())
 if requested_id:
  con=db();old=con.execute('SELECT user_id,assets_json,created_at FROM asset_projects WHERE id=?',(requested_id,)).fetchone();con.close()
  if old and old['user_id']!=user['id']:raise ValueError('不能覆盖其他账号的资产项目')
  if old:old_assets=json.loads(old['assets_json'] or '[]');created_at=old['created_at']
 project={'id':requested_id or uuid.uuid4().hex,'user_id':user['id'],'name':str(req.get('name') or data.get('projectTitle') or '未命名项目').strip()[:80],'source_type':str(req.get('sourceType','auto')),'source_text':source,'characters':generated_characters,'storyboard':{'logline':data.get('logline',''),'visualBible':data.get('visualBible',visual_style),'scenes':data.get('scenes') or [],'shots':shots,'aspect':aspect},'assets':old_assets,'status':'ready','error':'','created_at':created_at}
 save_asset_project(project);con=db();row=con.execute('SELECT * FROM asset_projects WHERE id=?',(project['id'],)).fetchone();con.close();return project_row(row)
def generate_asset_image(req,user):
 pid=str(req.get('projectId',''));item_id=str(req.get('itemId',''));asset_type=str(req.get('assetType',''))
 con=db();row=con.execute('SELECT * FROM asset_projects WHERE id=? AND user_id=?',(pid,user['id'])).fetchone();con.close()
 if not row:raise ValueError('资产项目不存在')
 project=project_row(row);items=project['characters'] if asset_type=='character' else project['storyboard'].get('scenes',[]) if asset_type=='scene' else []
 item=next((x for x in items if str(x.get('id'))==item_id),None)
 if not item:raise ValueError('要生成的资产不存在')
 if asset_type=='character' and item.get('referenceFile'):
  p=user_input(user)/Path(item['referenceFile']).name
  if p.is_file():return {'ok':True,'existing':True,'asset':{'id':item_id,'type':asset_type,'name':item.get('name'),'file':p.name,'url':'/api/media/input/'+quote(p.name)}}
 cfg=ai_config(user['id']);prompt=str(item.get('visualPrompt') or item.get('description') or '').strip()
 if not prompt:raise ValueError('当前资产缺少图片提示词')
 if asset_type=='character':prompt+='。角色设计参考板，单人，全身正侧背三视图与面部特写，纯净中性背景，无文字无水印，保持设定一致。'
 else:prompt+='。影视场景概念设计参考图，无人物，清晰展示空间布局、材质、光线和色彩，无文字无水印。'
 payload={'model':cfg['image_model'],'prompt':prompt,'n':1,'size':cfg.get('image_size') or '1536x1024','response_format':'b64_json'}
 try:answer=ai_request(cfg,'images/generations',payload,timeout=240)
 except ValueError as first:
  payload.pop('response_format',None)
  try:answer=ai_request(cfg,'images/generations',payload,timeout=240)
  except Exception:raise first
 try:image=answer['data'][0]
 except Exception:raise ValueError('图片模型响应中缺少 data[0]')
 raw=None;ext='.png'
 if image.get('b64_json'):raw=base64.b64decode(image['b64_json'])
 elif image.get('url'):
  with urllib.request.urlopen(image['url'],timeout=120) as r:
   raw=r.read(30*1024*1024+1);mime=r.headers.get_content_type();ext={'image/jpeg':'.jpg','image/webp':'.webp','image/png':'.png'}.get(mime,'.png')
 if not raw or len(raw)>30*1024*1024:raise ValueError('图片模型没有返回有效图片，或图片超过 30MB')
 stem=clean_filename(f"{project['name']}_{'人物' if asset_type=='character' else '场景'}_{item.get('name') or item_id}")
 root=user_input(user);dest=root/(stem+ext);counter=2
 while dest.exists():dest=root/f'{stem}_{counter}{ext}';counter+=1
 dest.write_bytes(raw);asset={'id':item_id,'type':asset_type,'name':item.get('name') or item_id,'file':dest.name,'url':'/api/media/input/'+quote(dest.name),'prompt':prompt}
 project['assets']=[x for x in project['assets'] if not (x.get('id')==item_id and x.get('type')==asset_type)]+[asset];save_asset_project(project);return {'ok':True,'asset':asset}
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
def validate_local_graph_nodes(graph):
 missing=[]
 for node_type in sorted({node['class_type'] for node in graph.values()}):
  try:info=comfy_get('/object_info/'+quote(node_type,safe=''))
  except Exception as exc:raise ValueError('无法检查 ComfyUI 工作流节点，请先启动本机 ComfyUI') from exc
  if node_type not in info:missing.append(node_type)
 if missing:raise ValueError('ComfyUI 缺少节点：'+'、'.join(missing))
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
  if node['class_type']=='UNETLoader':node['inputs']['unet_name']=H3_R2V_UNET if mode=='r2v' else H3_T2V_UNET
  if node['class_type']=='CLIPLoader':node['inputs']['clip_name']=H3_CLIP
  if node['class_type']=='PrimitiveFloat':node['inputs']['value']=duration
  if node['class_type']=='RandomNoise':node['inputs']['noise_seed']=int(req['seed']) if req.get('seed') not in (None,'') else time.time_ns()%1125899906842624
  if node['class_type']=='ResolutionSelector':
   allowed={'1:1 (Square)','2:3 (Portrait Photo)','3:2 (Photo)','3:4 (Portrait Standard)','4:3 (Standard)','9:16 (Portrait Widescreen)','16:9 (Widescreen)','21:9 (Ultrawide)'}
   node['inputs']['aspect_ratio']=req.get('aspect') if req.get('aspect') in allowed else '16:9 (Widescreen)'
   node['inputs']['megapixels']=max(.1,min(1.0,float(req.get('resolution') or .4)));node['inputs']['multiple']=32
 target=next(v for v in graph.values() if v['class_type'] in {'MiniMaxH3ImageToVideo','MiniMaxH3ReferenceToVideo'})
 target['inputs']['prompt']=prompt
 required_unet=H3_R2V_UNET if mode=='r2v' else H3_T2V_UNET
 if not (COMFY_ROOT/'models'/'diffusion_models'/required_unet).is_file():raise ValueError(f'本地模型缺失：{required_unet}。导演台不会联网下载，请通过国内镜像手动放入 ComfyUI/models/diffusion_models。')
 if not (COMFY_ROOT/'models'/'text_encoders'/H3_CLIP).is_file():raise ValueError(f'本地模型缺失：{H3_CLIP}。导演台不会联网下载，请通过国内镜像手动放入 ComfyUI/models/text_encoders。')
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
 graph.pop('170:160',None);graph['170:169']['inputs']['seed']=int(req['seed']) if req.get('seed') not in (None,'') else time.time_ns()%1125899906842624
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
  with THUMB_WORKERS:
   if not dest.exists() or not dest.stat().st_size:
    from PIL import Image, ImageOps
    temporary=THUMBS/(uuid.uuid4().hex+'.webp')
    try:
     with Image.open(p) as source:
      im=ImageOps.exif_transpose(source);im.thumbnail((480,480));im.convert('RGB').save(temporary,'WEBP',quality=72,method=4)
     temporary.replace(dest)
    finally:temporary.unlink(missing_ok=True)
  return self.file(dest,'image/webp')
 def video_thumb(self,p):
  key=hashlib.sha1((str(p)+str(p.stat().st_mtime_ns)).encode()).hexdigest()+'.jpg';dest=THUMBS/key
  with THUMB_WORKERS:
   if not dest.exists() or not dest.stat().st_size:
    temporary=THUMBS/(uuid.uuid4().hex+'.jpg')
    try:
     subprocess.run([str(FFMPEG),'-y','-threads','1','-i',str(p),'-frames:v','1','-vf','scale=480:480:force_original_aspect_ratio=decrease','-q:v','5',str(temporary)],check=True,capture_output=True,timeout=30)
     if not temporary.exists() or not temporary.stat().st_size:raise ValueError('视频未能提取预览帧')
     temporary.replace(dest)
    finally:temporary.unlink(missing_ok=True)
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
  if path=='/api/v2/discovery':
   status,payload=V2_API.public_get(path,urlsplit(self.path).query);return self.out(status,payload)
  if path.startswith('/api/v2/public-preview/'):
   try:
    context=V2_API.public_preview_context(path.rsplit('/',1)[-1],input_root_for_user=user_input,output_root_for_user=result_root)
    if context['kind']=='image':return self.thumb(context['path'])
    if context['kind']=='video':return self.video_thumb(context['path'])
    return self.out(404,{'error':'此媒体没有公开图像预览'})
   except Exception as e:return self.out(404,{'error':str(e)})
  if path=='/character-replace-portfolio.html':
   f=(WEB/'character-replace-portfolio.html').resolve()
   if WEB not in f.parents or not f.is_file():return self.out(404,{'error':'Not found'})
   return self.file(f,'text/html; charset=utf-8')
  if path.startswith('/portfolio-media/character-replace/'):
   public_root=(WEB/'portfolio-media'/'character-replace').resolve();rel=urllib.parse.unquote(path[len('/portfolio-media/character-replace/'):]);f=(public_root/rel).resolve()
   if public_root not in f.parents or not f.is_file():return self.out(404,{'error':'Not found'})
   return self.file(f)
  if path in {'/discover.html','/discover.css','/discover.js','/studio-v2.css'}:
   f=(WEB/path.lstrip('/')).resolve()
   if WEB not in f.parents or not f.is_file():return self.out(404,{'error':'Not found'})
   return self.out(200,f.read_bytes(),mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
  if path in {'/','/login.html'} and not self.current_user():
   f=WEB/'login.html';content=f.read_bytes();requested=urllib.parse.parse_qs(urlsplit(self.path).query).get('next',['/'])[0]
   next_path=safe_login_target(requested)
   content=content.replace(b"location.href='/studio.html'",('location.href='+json.dumps(next_path)).encode(),2)
   return self.out(200,content,'text/html; charset=utf-8')
  if path in {'/studio.html','/assets.html','/account.html','/settings.html'} and not self.current_user():
   return self.out(302,b'','text/plain; charset=utf-8',headers={'Location':'/login.html?next='+quote(path,safe='')})
  user=self.require_user()
  if not user:return
  if path.startswith('/api/v2/'):
   if path=='/api/v2/legacy-history':
    try:
     query=urllib.parse.parse_qs(urlsplit(self.path).query);offset=max(0,int(query.get('offset',['0'])[0]));limit=min(100,max(1,int(query.get('limit',['30'])[0])))
     records=json.loads(JOBS.read_text(encoding='utf-8')) if JOBS.exists() else {}
     if not isinstance(records,dict):raise ValueError('旧历史格式异常')
     return self.out(200,legacy_history(records,user,query.get('q',[''])[0],offset,limit))
    except (ValueError,OSError):return self.out(400,{'error':'旧历史读取失败，请检查原历史文件'})
   if path=='/api/v2/workflows':return self.out(200,workflow_catalog(API_DIR/'qwen_image_edit_2511_api.json',CHARACTER_WORKFLOW,COMFY_ROOT))
   if path=='/api/v2/tasks':reconcile_v2_tasks(user)
   status,payload=V2_API.get(path,user,urlsplit(self.path).query)
   return self.out(status,payload)
  if self.path.startswith('/api/comfy'):
   if user['role']!='admin':return self.out(403,{'error':'仅管理员可访问 ComfyUI 代理'})
   return self.proxy('GET')
  if self.path=='/api/library':return self.out(200,self.library())
  if self.path=='/api/results':return self.out(200,self.results())
  if self.path=='/api/ai-config':
   cfg=ai_config(user['id']);return self.out(200,{'configured':bool(cfg and cfg.get('api_key')),'baseUrl':cfg.get('base_url','') if cfg else 'https://api.openai.com/v1','textModel':cfg.get('text_model','') if cfg else 'gpt-5-mini','imageModel':cfg.get('image_model','') if cfg else 'gpt-image-1.5','classificationModel':(cfg.get('classification_model') or cfg.get('text_model','')) if cfg else 'gpt-5-mini','embeddingModel':cfg.get('embedding_model','') if cfg else 'text-embedding-3-small','imageSize':cfg.get('image_size','1536x1024') if cfg else '1536x1024','keyHint':('••••'+cfg['api_key'][-4:]) if cfg and cfg.get('api_key') else ''})
  if self.path=='/api/asset-projects':return self.out(200,list_asset_projects(user['id']))
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
  if path=='/index.html':return self.out(302,b'','text/plain; charset=utf-8',headers={'Location':'/studio.html'})
  f=(WEB/("studio.html" if path=='/' else path.lstrip('/'))).resolve()
  if WEB not in f.parents or not f.is_file():return self.out(404,{"error":"Not found"})
  content=f.read_bytes()
  if f.name=='account.html':content=content.replace('所有公共账号共享公共素材库与结果库。'.encode(), '同一公共账号内共享素材和结果；不同公共账号之间相互隔离。'.encode())
  self.out(200,content,mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
 def do_POST(self):
  if self.path in {'/api/auth/register','/api/auth/login'}:return self.auth_post()
  user=self.require_user()
  if not user:return
  if not self.check_csrf(user):return self.out(403,{'error':'安全令牌无效，请刷新页面后重试'})
  path=urlsplit(self.path).path
  if path.startswith('/api/v2/'):
   try:req=json.loads(self.body() or b'{}')
   except Exception:return self.out(400,{'error':'请求格式错误'})
   if not isinstance(req,dict):return self.out(400,{'error':'请求必须是 JSON 对象'})
   if path in {'/api/v2/tasks/mark','/api/v2/versions/register'}:
    return self.out(403,{'error':'任务执行状态和结果只能由服务端登记'})
   if path=='/api/v2/projects/analyze':
    try:return self.out(200,analyze_v2_project(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   if path=='/api/v2/generation/submit':
    try:return self.out(200,submit_v2_generation(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   if path=='/api/v2/generation/cancel':
    try:return self.out(200,cancel_v2_task(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   if path=='/api/v2/generation/fail-stalled':
    try:return self.out(200,fail_stalled_v2_task(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   if path=='/api/v2/media/semantic-search':
    try:return self.out(200,run_v2_semantic_search(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   if path=='/api/v2/classifications/run':
    try:return self.out(200,run_v2_classification(req,user))
    except Exception as e:return self.out(400,{'error':str(e)})
   status,payload=V2_API.post(path,user,req,input_root=user_input(user),output_root=result_root(user))
   return self.out(status,payload)
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
  if self.path=='/api/ai-config':
   try:
    req=json.loads(self.body() or b'{}');old=ai_config(user['id']) or {};base_url=str(req.get('baseUrl','')).strip().rstrip('/');api_key=str(req.get('apiKey','')).strip() or old.get('api_key','');text_model=str(req.get('textModel','')).strip();image_model=str(req.get('imageModel','')).strip();classification_model=str(req.get('classificationModel',old.get('classification_model',''))).strip();embedding_model=str(req.get('embeddingModel',old.get('embedding_model',''))).strip();image_size=str(req.get('imageSize','1536x1024')).strip()
    if not base_url.startswith(('http://','https://')):raise ValueError('Base URL 必须以 http:// 或 https:// 开头')
    if not api_key or not text_model or not image_model:raise ValueError('请填写 API Key、文本模型和图片模型')
    if image_size not in {'1024x1024','1536x1024','1024x1536','auto'}:raise ValueError('不支持的图片尺寸')
    con=db();con.execute('INSERT INTO user_ai_configs(user_id,base_url,api_key,text_model,image_model,image_size,updated_at,classification_model,embedding_model) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET base_url=excluded.base_url,api_key=excluded.api_key,text_model=excluded.text_model,image_model=excluded.image_model,image_size=excluded.image_size,classification_model=excluded.classification_model,embedding_model=excluded.embedding_model,updated_at=excluded.updated_at',(user['id'],base_url,api_key,text_model,image_model,image_size,int(time.time()),classification_model,embedding_model));con.commit();con.close();return self.out(200,{'ok':True,'keyHint':'••••'+api_key[-4:]})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/ai-config/test':
   try:
    req=json.loads(self.body() or b'{}');saved=ai_config(user['id']) or {};cfg={'base_url':str(req.get('baseUrl') or saved.get('base_url','')).strip().rstrip('/'),'api_key':str(req.get('apiKey') or saved.get('api_key','')).strip(),'text_model':str(req.get('textModel') or saved.get('text_model','')).strip()};answer=ai_request(cfg,'chat/completions',{'model':cfg['text_model'],'messages':[{'role':'user','content':'只回复 OK'}],'temperature':0,'max_tokens':8},timeout=30);return self.out(200,{'ok':True,'reply':str(answer.get('choices',[{}])[0].get('message',{}).get('content','OK'))[:80]})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/asset-projects/generate':
   try:return self.out(200,analyze_asset_project(json.loads(self.body() or b'{}'),user))
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/asset-projects/image':
   try:return self.out(200,generate_asset_image(json.loads(self.body() or b'{}'),user))
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/asset-projects/delete':
   try:
    req=json.loads(self.body() or b'{}');con=db();cur=con.execute('DELETE FROM asset_projects WHERE id=? AND user_id=?',(str(req.get('id','')),user['id']));con.commit();con.close()
    if not cur.rowcount:raise ValueError('项目不存在')
    return self.out(200,{'ok':True})
   except Exception as e:return self.out(400,{'error':str(e)})
  if self.path=='/api/generate-storyboard':
   try:
    req=json.loads(self.body() or b'{}');shot_rows=[x for x in req.get('shots',[]) if str(x.get('text','')).strip()][:40]
    if not shot_rows:raise ValueError('请至少填写一个分镜')
    variants=1 if user['role']=='user' else max(1,min(4,int(req.get('count') or 1)));total=len(shot_rows)*variants;ensure_quota(user,'video',total);answers=[];all_jobs=jobs();base_title=str(req.get('title') or '分镜导演').strip();overall=str(req.get('prompt') or '').strip()
    for shot_index,shot in enumerate(shot_rows,1):
     for variant in range(variants):
      item=dict(req);item['count']=1;item['shots']=[];item['duration']=max(5,min(15,float(shot.get('seconds') or 5)));item['title']=f'{base_title} · 镜头 {shot_index:02d}';item['prompt']='\n\n'.join(x for x in (overall,f"分镜 {shot_index}：{str(shot.get('text','')).strip()}") if x)
      graph,prefix,duration=workflow(item,user);prefix=f'{prefix}_v{variant+1}' if variants>1 else prefix;graph['92']['inputs']['filename_prefix']=prefix;answer=comfy_post('/prompt',{'prompt':graph});answers.append(answer.get('prompt_id'));all_jobs[prefix]=dict(item,duration=duration,user_id=user['id'],username=user['username'],storyboard_index=shot_index);save_jobs(all_jobs);consume(user,'video',1)
    return self.out(200,{'ok':True,'prompt_ids':answers,'count':total,'shots':len(shot_rows),'variants':variants})
   except urllib.error.HTTPError as e:
    try:detail=json.loads(e.read())
    except Exception:detail=str(e)
    return self.out(400,{'error':'分镜工作流未通过 ComfyUI 校验','detail':detail})
   except Exception as e:return self.out(400,{'error':str(e)})
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
