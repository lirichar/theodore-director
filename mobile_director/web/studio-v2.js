const $=selector=>document.querySelector(selector);
const $$=selector=>[...document.querySelectorAll(selector)];
const state={csrf:'',bootstrap:null,projects:[],items:[],total:0,offset:0,limit:60,kind:'',query:'',projectId:'',lifecycle:'active',selected:null,selectedIds:new Set(),draft:null,compact:false,mediaRequest:0,selectionRequest:0,mediaCache:new Map()};

function escapeHtml(value){const node=document.createElement('div');node.textContent=value??'';return node.innerHTML}
function toast(message){const node=$('#toast');node.textContent=message;node.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.remove('show'),3000)}
function humanSize(bytes){const value=Number(bytes)||0;if(value<1024)return `${value} B`;if(value<1048576)return `${(value/1024).toFixed(1)} KB`;if(value<1073741824)return `${(value/1048576).toFixed(1)} MB`;return `${(value/1073741824).toFixed(1)} GB`}
function sourceLabel(source){return({legacy_input:'上传素材',legacy_result:'生成结果',upload:'上传素材',generation:'生成结果'})[source]||source||'未知'}
function csv(value){return String(value||'').split(/[，,]/).map(x=>x.trim()).filter(Boolean)}
function setBusy(button,busy,label='处理中…'){if(!button)return;if(busy){if(button.dataset.busy==='true')return;button.dataset.old=button.textContent;button.dataset.wasDisabled=String(button.disabled);button.dataset.busy='true';button.disabled=true;button.textContent=label}else{button.disabled=button.dataset.wasDisabled==='true';button.textContent=button.dataset.old||button.textContent;delete button.dataset.busy}}
function cacheMedia(items){for(const item of items||[])state.mediaCache.set(item.id,item)}
function mediaVisual(item){return item.preview?`<img src="${escapeHtml(item.preview)}" data-media-preview="${escapeHtml(item.id)}" loading="lazy" decoding="async" alt="素材预览">`:`<span class="media-icon">${item.kind==='audio'?'♫':'▧'}</span>`}
function wireMediaFallbacks(root){root.querySelectorAll('img[data-media-preview]').forEach(img=>{img.onerror=()=>{const item=state.mediaCache.get(img.dataset.mediaPreview);if(!item)return;img.onerror=null;if(item.kind==='image'&&!img.dataset.original){img.dataset.original='true';img.src=item.url;img.onerror=()=>showPreviewRetry(img,item)}else if(item.kind==='video'){const video=document.createElement('video');video.src=item.url;video.preload='metadata';video.muted=true;video.playsInline=true;video.onerror=()=>showPreviewRetry(video,item);img.replaceWith(video)}else showPreviewRetry(img,item)}})}
function showPreviewRetry(node,item){const retry=document.createElement('span');retry.className='preview-retry';retry.textContent='预览加载失败 · 点击重试';retry.tabIndex=0;const reload=event=>{event.stopPropagation();const img=document.createElement('img');img.dataset.mediaPreview=item.id;img.src=item.preview;retry.replaceWith(img);wireMediaFallbacks(img.parentElement)};retry.onclick=reload;retry.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();reload(event)}};node.replaceWith(retry)}
function openFullPreview(item){let dialog=$('#mediaPreviewDialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='mediaPreviewDialog';dialog.className='full-media-dialog';document.body.append(dialog);dialog.addEventListener('close',()=>{dialog.querySelectorAll('video,audio').forEach(media=>media.pause());dialog.innerHTML=''});dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close()})}dialog.innerHTML=`<header><b>${escapeHtml(item.name)}</b><button type="button" aria-label="关闭完整预览">×</button></header><div class="full-media-body">${item.kind==='image'?`<img src="${escapeHtml(item.url)}" alt="完整图片">`:item.kind==='video'?`<video src="${escapeHtml(item.url)}" controls playsinline preload="metadata"></video>`:`<audio src="${escapeHtml(item.url)}" controls></audio>`}</div>`;dialog.querySelector('button').onclick=()=>dialog.close();dialog.showModal()}
function refreshInspectorActions(){const item=state.selected;if(!item)return;const archived=state.draft?.status==='archived',active=item.state==='active',busy=id=>$(id).dataset.busy==='true';$('#promoteMedia').disabled=busy('#promoteMedia')||!active||item.links.some(x=>x.targetType==='workspace'&&x.role==='reusable_asset');$('#saveDraft').disabled=busy('#saveDraft')||!active||archived;$('#archiveDraft').disabled=busy('#archiveDraft')||!active||archived;$('#autoClassify').disabled=busy('#autoClassify')||!active;$('#publishMedia').disabled=busy('#publishMedia')||!active||!state.hasArchived;$('#linkProjectButton').disabled=busy('#linkProjectButton')||!active;['fieldLabel','fieldCaption','fieldBest','fieldAvoid','fieldNotes','fieldKeywords'].forEach(id=>$('#'+id).readOnly=archived||!active)}

async function api(url,options={}){
  if(options.method&&options.method!=='GET')options.headers={...(options.headers||{}),'X-CSRF-Token':state.csrf};
  const response=await fetch(url,options);const text=await response.text();let data;
  try{data=JSON.parse(text)}catch{data={error:text||`HTTP ${response.status}`}}
  if(response.status===401){location.href='/login.html';throw new Error('登录已过期')}
  if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);return data;
}
function post(url,body){return api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})}

function renderProjects(){
  const options=state.projects.map(project=>`<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join('');
  $('#projectFilter').innerHTML='<option value="">所有项目</option>'+options;
  $('#linkProject').innerHTML='<option value="">选择项目</option>'+options;
  $('#bulkProject').innerHTML='<option value="">选择关联项目</option>'+options;
  $('#projectFilter').value=state.projectId;
}

function mediaCard(item){
  const label=item.retrieval?.displayLabel||item.name;
  const description=item.retrieval?.searchCaption||sourceLabel(item.source);
  const reusable=item.links?.some(link=>link.targetType==='workspace'&&link.role==='reusable_asset');
  const visual=mediaVisual(item);
  return `<article class="asset-card ${state.selected?.id===item.id?'selected':''} ${state.selectedIds.has(item.id)?'batch-selected':''}" data-id="${escapeHtml(item.id)}" tabindex="0">
    <div class="asset-thumb">${visual}<label class="asset-check" title="选择素材"><input type="checkbox" data-select-media="${escapeHtml(item.id)}" ${state.selectedIds.has(item.id)?'checked':''} aria-label="选择 ${escapeHtml(label)}"><span>✓</span></label><span class="kind">${escapeHtml(item.kind)}</span>${reusable?'<span class="reusable">可复用</span>':''}</div>
    <div class="asset-body"><h3>${escapeHtml(label)}</h3><p>${escapeHtml(description)}</p><div class="tag-chips">${(item.tags||[]).slice(0,5).map(tag=>`<span>${escapeHtml(tag)}</span>`).join('')}</div><footer><span>${escapeHtml(sourceLabel(item.source))}</span><span>${humanSize(item.size)}</span></footer></div>
  </article>`;
}

function renderMedia(){
  const grid=$('#assetGrid');grid.classList.toggle('compact',state.compact);grid.innerHTML=state.items.map(mediaCard).join('');
  $('#contentState').hidden=state.items.length>0;
  if(!state.items.length){$('#contentState').innerHTML='<div style="font-size:30px;color:#aaa">▧</div><p>没有符合条件的素材</p><button class="secondary" id="emptySync">扫描现有素材</button>';$('#emptySync').onclick=syncCatalog}
  wireMediaFallbacks(grid);
  $('#mediaCount').textContent=state.total.toLocaleString();$('#resultCount').textContent=`${state.total.toLocaleString()} 项素材`;
  $('#loadMore').hidden=state.items.length>=state.total;
  $$('.asset-card').forEach(card=>{card.onclick=event=>{if(event.target.closest('.asset-check'))return;selectMedia(card.dataset.id)};card.onkeydown=event=>{if((event.key==='Enter'||event.key===' ')&&!event.target.closest('.asset-check')){event.preventDefault();selectMedia(card.dataset.id)}}});
  $$('[data-select-media]').forEach(input=>input.onchange=event=>{event.stopPropagation();toggleMediaSelection(input.dataset.selectMedia,input.checked)});
  renderBulkToolbar();
}

function toggleMediaSelection(id,checked){checked?state.selectedIds.add(id):state.selectedIds.delete(id);renderMedia()}
function renderBulkToolbar(){const count=state.selectedIds.size,trash=state.lifecycle==='trash';$('#bulkToolbar').hidden=!count;$('#bulkCount').textContent=`已选择 ${count} 项`;$('#bulkTrash').hidden=trash;$('#bulkRestore').hidden=!trash;$('#bulkDelete').hidden=!trash;$('#bulkProject').hidden=trash;$('#bulkLink').hidden=trash}
function clearMediaSelection(){state.selectedIds.clear();renderMedia()}

async function loadMedia(reset=true){
  const request=++state.mediaRequest;
  if(reset){state.offset=0;state.items=[];$('#contentState').hidden=false;$('#contentState').innerHTML='<div class="spinner"></div><p>正在检索素材…</p>'}
  const query=new URLSearchParams({limit:String(state.limit),offset:String(state.offset)});
  if(state.query)query.set('q',state.query);if(state.kind)query.set('kind',state.kind);if(state.projectId)query.set('projectId',state.projectId);query.set('state',state.lifecycle);
  try{const data=await api('/api/v2/media?'+query);if(request!==state.mediaRequest)return;cacheMedia(data.items);state.items=reset?data.items:[...new Map([...state.items,...data.items].map(item=>[item.id,item])).values()];state.total=data.total;state.offset=state.items.length;renderMedia()}
  catch(error){if(request!==state.mediaRequest)return;$('#contentState').hidden=false;$('#contentState').innerHTML=`<p>${escapeHtml(error.message)}</p>`;toast(error.message)}
}

function currentDescriptor(){
  return {
    displayLabel:$('#fieldLabel').value.trim(),searchCaption:$('#fieldCaption').value.trim(),
    bestFor:csv($('#fieldBest').value),avoidFor:csv($('#fieldAvoid').value),
    usageNotes:$('#fieldNotes').value.trim(),keywords:csv($('#fieldKeywords').value),
  };
}
function fillDescriptor(descriptor={}){
  $('#fieldLabel').value=descriptor.displayLabel||'';$('#fieldCaption').value=descriptor.searchCaption||'';
  $('#fieldBest').value=(descriptor.bestFor||[]).join('，');$('#fieldAvoid').value=(descriptor.avoidFor||[]).join('，');
  $('#fieldNotes').value=descriptor.usageNotes||'';$('#fieldKeywords').value=(descriptor.keywords||[]).join('，');updateWatermark();
}
function updateWatermark(){const descriptor=currentDescriptor();$('#watermark').textContent=[descriptor.displayLabel,descriptor.searchCaption].filter(Boolean).join(' · ')||'尚未生成 AI 检索描述';$('#watermark').hidden=!$('#watermarkToggle').checked}

async function selectMedia(id){
  const request=++state.selectionRequest;
  try{
    const data=await api('/api/v2/media/'+encodeURIComponent(id));if(request!==state.selectionRequest)return;state.selected=data.media;cacheMedia([data.media]);state.hasArchived=data.classifications.some(x=>x.status==='archived');state.draft=data.classifications.find(x=>x.status==='draft')||data.classifications[0]||null;
    renderMedia();$('#inspectorEmpty').hidden=true;$('#inspectorContent').hidden=false;$('#inspector').classList.add('open');
    $('#detailName').textContent=state.selected.retrieval?.displayLabel||state.selected.name;$('#detailId').textContent=state.selected.id;$('#detailSource').textContent=sourceLabel(state.selected.source);$('#detailSize').textContent=humanSize(state.selected.size);
    const preview=$('#detailPreview');preview.querySelectorAll('img,video,.media-icon,span').forEach(node=>node.remove());
    if(state.selected.kind==='image')preview.insertAdjacentHTML('afterbegin',`<img src="${escapeHtml(state.selected.url)}" alt="">`);
    else if(state.selected.kind==='video')preview.insertAdjacentHTML('afterbegin',`<video src="${escapeHtml(state.selected.url)}" controls preload="metadata"></video>`);
    else preview.insertAdjacentHTML('afterbegin','<span class="media-icon">♫</span>');
    $('#linkCount').textContent=`${state.selected.links.length} 个关联`;$('#linkList').innerHTML=state.selected.links.length?state.selected.links.map(link=>`<span class="link-chip">${escapeHtml(link.targetType)} · ${escapeHtml(link.role)} <button data-unlink="${escapeHtml(link.id)}" title="移除关联">×</button></span>`).join(''):'<span class="link-chip">尚未关联</span>';$$('[data-unlink]').forEach(button=>button.onclick=async()=>{try{await post('/api/v2/media/unlink',{mediaId:state.selected.id,linkId:button.dataset.unlink});toast('已移除关联，原文件保留');await selectMedia(state.selected.id)}catch(error){toast(error.message)}});
    const reusable=state.selected.links.some(link=>link.targetType==='workspace'&&link.role==='reusable_asset');$('#promoteMedia').textContent=reusable?'已是可复用素材':'标记为可复用素材';$('#promoteMedia').disabled=reusable;
    $('#trashMedia').textContent=state.selected.state==='trash'?'恢复素材':'移入回收站';
    $('#deleteMedia').hidden=state.selected.state!=='trash';
    const classification=state.draft?.classification||{retrievalDescriptor:state.selected.retrieval||{}};fillDescriptor(classification.retrievalDescriptor||{});
    const status=state.draft?.status||'未分类';$('#classStatus').textContent=status==='draft'?'待确认':status==='archived'?'已归档':'未分类';$('#classStatus').className='status-pill '+(status==='draft'?'draft':status==='archived'?'archived':'');
    $('#saveDraft').disabled=status==='archived';$('#archiveDraft').disabled=status==='archived';
    $('#mediaTags').value=(state.selected.tags||[]).join('，');$('#mediaTags').disabled=state.selected.state!=='active';$('#saveTags').disabled=state.selected.state!=='active';$('#tagChips').innerHTML=(state.selected.tags||[]).map(tag=>`<span>${escapeHtml(tag)}</span>`).join('');
    $('#fullPreview').onclick=()=>openFullPreview(state.selected);
    refreshInspectorActions();
  }catch(error){if(request===state.selectionRequest)toast(error.message)}
}

async function syncCatalog(){const button=$('#syncCatalog');setBusy(button,true,'扫描中…');try{const data=await post('/api/v2/catalog/sync',{});toast(`已索引 ${data.seen} 项，新增 ${data.created} 项；未复制文件`);await loadMedia()}catch(error){toast(error.message)}finally{setBusy(button,false)}}

$('#syncCatalog').onclick=syncCatalog;
$('#saveTags').onclick=async()=>{if(!state.selected)return;const id=state.selected.id,button=$('#saveTags');setBusy(button,true,'保存中…');try{await post('/api/v2/media/tags',{mediaId:id,tags:csv($('#mediaTags').value)});await loadMedia();if(state.selected?.id===id)await selectMedia(id);toast('标签已保存，无需关联项目')}catch(error){toast(error.message)}finally{setBusy(button,false)}};
$('#semanticSearch').onclick=async()=>{const query=$('#search').value.trim();if(!query)return toast('先输入想找的内容或用途');if(state.lifecycle!=='active'||state.kind||state.projectId)return toast('语义检索当前检索全部在用素材；如需限定分类、项目或回收站，请使用关键词搜索');clearTimeout(loadMedia.timer);const request=++state.mediaRequest,button=$('#semanticSearch');setBusy(button,true,'检索中…');try{const data=await post('/api/v2/media/semantic-search',{query,limit:100});if(request!==state.mediaRequest)return;cacheMedia(data.items);state.items=data.items;state.total=data.total;state.offset=data.items.length;renderMedia();$('#loadMore').hidden=true;toast(data.method==='semantic'?`语义检索完成${data.embedded?`，更新 ${data.embedded} 项索引`:''}`:data.warning)}catch(error){if(request===state.mediaRequest)toast(error.message)}finally{setBusy(button,false)}};
$('#search').oninput=event=>{clearTimeout(loadMedia.timer);state.query=event.target.value.trim();loadMedia.timer=setTimeout(()=>loadMedia(),240)};
$('#kindFilters').onclick=event=>{const button=event.target.closest('button');if(!button)return;$$('#kindFilters button').forEach(node=>node.classList.toggle('active',node===button));state.kind=button.dataset.kind;loadMedia()};
$('#projectFilter').onchange=event=>{state.projectId=event.target.value;loadMedia()};
$('#trashView').onclick=()=>{state.lifecycle=state.lifecycle==='trash'?'active':'trash';state.selectedIds.clear();state.selected=null;$('#inspectorContent').hidden=true;$('#inspectorEmpty').hidden=false;$('#trashView').textContent=state.lifecycle==='trash'?'返回素材':'回收站';loadMedia()};
$('#viewToggle').onclick=()=>{state.compact=!state.compact;renderMedia()};$('#loadMore').onclick=()=>loadMedia(false);
$('#closeInspector').onclick=()=>{++state.selectionRequest;$('#inspector').classList.remove('open')};
$('#renameMedia').onclick=async()=>{if(!state.selected)return;const name=prompt('新的显示名称',state.selected.name);if(!name)return;try{await post('/api/v2/media/rename',{mediaId:state.selected.id,displayName:name});toast('显示名称已更新，文件路径和引用未改变');await loadMedia();await selectMedia(state.selected.id)}catch(error){toast(error.message)}};
$('#trashMedia').onclick=async()=>{if(!state.selected)return;const restoring=state.selected.state==='trash';if(!restoring&&!confirm('移入回收站？项目与版本引用会保留，可随时恢复。'))return;try{await post(restoring?'/api/v2/media/restore':'/api/v2/media/trash',{mediaId:state.selected.id});toast(restoring?'素材已恢复':'已移入回收站，原文件尚未删除');state.selected=null;$('#inspectorContent').hidden=true;$('#inspectorEmpty').hidden=false;await loadMedia()}catch(error){toast(error.message)}};
$('#deleteMedia').onclick=async()=>{if(!state.selected||state.selected.state!=='trash')return;if(!confirm(`彻底删除“${state.selected.name}”？\n文件与素材记录将不可恢复。仍有引用时系统会拒绝删除。`))return;const id=state.selected.id,button=$('#deleteMedia');setBusy(button,true,'删除中…');try{await post('/api/v2/media/permanent-delete',{mediaId:id});state.selectedIds.delete(id);state.selected=null;$('#inspectorContent').hidden=true;$('#inspectorEmpty').hidden=false;await loadMedia();toast('素材已彻底删除，不可恢复')}catch(error){toast(error.message)}finally{setBusy(button,false)}};
$('#watermarkToggle').onchange=updateWatermark;['fieldLabel','fieldCaption'].forEach(id=>$('#'+id).oninput=updateWatermark);

async function runBulkMediaAction(action){
  const mediaIds=[...state.selectedIds];if(!mediaIds.length)return;
  const projectId=$('#bulkProject').value;
  if(action==='link_project'&&!projectId)return toast('请先选择要关联的项目');
  if(action==='trash'&&!confirm(`将选中的 ${mediaIds.length} 项素材移入回收站？`))return;
  if(action==='permanent_delete'&&!confirm(`彻底删除选中的 ${mediaIds.length} 项素材？\n文件与素材记录将不可恢复；仍有引用的素材会保留。`))return;
  const button=action==='link_project'?$('#bulkLink'):action==='trash'?$('#bulkTrash'):action==='restore'?$('#bulkRestore'):$('#bulkDelete');setBusy(button,true,'处理中…');
  try{
    const result=await post('/api/v2/media/bulk',{action,mediaIds,projectId});
    if(action==='link_project'){toast(`已关联 ${result.succeeded} 项，没有复制文件`);await loadMedia();return}
    state.selectedIds.clear();state.selected=null;$('#inspectorContent').hidden=true;$('#inspectorEmpty').hidden=false;await loadMedia();
    const label=action==='trash'?'移入回收站':action==='restore'?'恢复':'彻底删除';toast(`已${label} ${result.succeeded} 项${result.failed?`，${result.failed} 项受保护`:''}`);
    if(result.failures?.length)alert('以下素材未删除：\n'+result.failures.slice(0,12).map(item=>`${item.mediaId.slice(0,12)}…：${item.error}`).join('\n'));
  }catch(error){toast(error.message)}finally{setBusy(button,false)}
}
$('#selectLoaded').onclick=()=>{state.items.forEach(item=>state.selectedIds.add(item.id));renderMedia()};
$('#clearSelection').onclick=clearMediaSelection;
$('#bulkLink').onclick=()=>runBulkMediaAction('link_project');
$('#bulkTrash').onclick=()=>runBulkMediaAction('trash');
$('#bulkRestore').onclick=()=>runBulkMediaAction('restore');
$('#bulkDelete').onclick=()=>runBulkMediaAction('permanent_delete');

$('#promoteMedia').onclick=async()=>{if(!state.selected)return;const id=state.selected.id,button=$('#promoteMedia');setBusy(button,true,'处理中…');try{const result=await post('/api/v2/media/promote',{mediaId:id});toast(result.copied?'已创建副本':'已建立复用关系，没有复制文件');await loadMedia();if(state.selected?.id===id)await selectMedia(id)}catch(error){toast(error.message)}finally{setBusy(button,false);refreshInspectorActions()}};
$('#linkProjectButton').onclick=async()=>{const projectId=$('#linkProject').value;if(!state.selected)return;if(!projectId)return toast('请先选择项目');const id=state.selected.id,button=$('#linkProjectButton');setBusy(button,true,'关联中…');try{await post('/api/v2/media/link',{mediaId:id,targetType:'project',targetId:projectId,role:'reference'});toast('已关联到项目，没有复制文件');if(state.selected?.id===id)await selectMedia(id)}catch(error){toast(error.message)}finally{setBusy(button,false);refreshInspectorActions()}};
$('#publishMedia').onclick=async()=>{if(!state.selected)return;const button=$('#publishMedia');setBusy(button,true,'发布中…');try{const descriptor=currentDescriptor();await post('/api/v2/shares/publish',{mediaId:state.selected.id,title:descriptor.displayLabel||state.selected.name,permissions:{platformUse:true,remix:true,attributionRequired:true,originalDownload:false},recipe:{prompt:'',aspect:'16:9'}});toast('已发布：公开预览可浏览，原图不可下载');button.dataset.old='已发布到开放素材库'}catch(error){toast(error.message)}finally{setBusy(button,false)}};

$('#autoClassify').onclick=async()=>{if(!state.selected)return;const id=state.selected.id,button=$('#autoClassify');setBusy(button,true,'AI 正在分析…');try{const draft=await post('/api/v2/classifications/run',{mediaId:id});if(state.selected?.id===id){state.draft=draft;fillDescriptor(draft.classification.retrievalDescriptor||{});$('#classStatus').textContent='待确认';$('#classStatus').className='status-pill draft'}toast('分类结果已返回，可编辑后确认归档')}catch(error){toast(error.message)}finally{setBusy(button,false);refreshInspectorActions()}};
function editedClassification(){return {...(state.draft?.classification||{}),retrievalDescriptor:currentDescriptor()}}
async function ensureDraft(mediaId,classification,draft){
  if(draft?.status==='draft')return draft;
  return post('/api/v2/classifications/draft',{mediaId,provider:'manual',classification});
}
$('#saveDraft').onclick=async()=>{if(!state.selected)return;const id=state.selected.id,classification=editedClassification(),existing=state.draft,button=$('#saveDraft');setBusy(button,true,'保存中…');try{const draft=await ensureDraft(id,classification,existing),saved=await post('/api/v2/classifications/edit',{revisionId:draft.revisionId,classification});if(state.selected?.id===id){state.draft=saved;$('#classStatus').textContent='待确认';$('#classStatus').className='status-pill draft'}toast('分类草稿已保存')}catch(error){toast(error.message)}finally{setBusy(button,false);refreshInspectorActions()}};
$('#archiveDraft').onclick=async()=>{if(!state.selected)return;const id=state.selected.id,classification=editedClassification(),existing=state.draft,button=$('#archiveDraft');setBusy(button,true,'归档中…');try{const draft=await ensureDraft(id,classification,existing);await post('/api/v2/classifications/confirm',{revisionId:draft.revisionId,classification});toast('已确认归档，检索描述正式生效');await loadMedia();if(state.selected?.id===id)await selectMedia(id)}catch(error){toast(error.message)}finally{setBusy(button,false);refreshInspectorActions()}};

const modal=$('#projectModal');$('#newProject').onclick=()=>{modal.hidden=false;$('#projectName').focus()};$$('[data-close]').forEach(button=>button.onclick=()=>modal.hidden=true);modal.onclick=event=>{if(event.target===modal)modal.hidden=true};
$('#projectForm').onsubmit=async event=>{event.preventDefault();const button=event.submitter;setBusy(button,true,'创建中…');try{const data=await post('/api/v2/projects',{name:$('#projectName').value,description:$('#projectDescription').value,aspect:$('#projectAspect').value});state.projects.unshift(data.project);renderProjects();modal.hidden=true;event.target.reset();toast('项目已创建')}catch(error){toast(error.message)}finally{setBusy(button,false)}};

$('#openUpload').onclick=()=>$('#uploadInput').click();$('#uploadInput').onchange=async event=>{const files=[...event.target.files];if(!files.length)return;const button=$('#openUpload');setBusy(button,true,'上传中…');try{for(const file of files){const form=new FormData();form.append('file',file);await api('/api/upload',{method:'POST',body:form})}await post('/api/v2/catalog/sync',{});await loadMedia();toast(`${files.length} 项素材已上传并进入统一目录`)}catch(error){toast(error.message)}finally{event.target.value='';setBusy(button,false)}};
$('#mainNav').onclick=event=>{const button=event.target.closest('button');if(!button||button.dataset.view==='assets')return;if(button.dataset.view==='discovery'){location.href='/discover.html';return}toast('本轮先验收统一素材目录；该模块会按 SDD 分片接入')};

async function init(){
  try{
    const me=await fetch('/api/auth/me').then(response=>response.json());if(!me.authenticated){location.href='/login.html';return}state.csrf=me.csrf;localStorage.setItem('td-csrf',me.csrf);$('#avatar').textContent=(me.username||'?').slice(0,1).toUpperCase();
    state.bootstrap=await api('/api/v2/bootstrap');state.projects=state.bootstrap.projects;$('#workspaceName').textContent=state.bootstrap.workspace.name;renderProjects();
    if(state.bootstrap.counts.media===0)await post('/api/v2/catalog/sync',{});await loadMedia();
  }catch(error){toast(error.message);$('#contentState').innerHTML=`<p>${escapeHtml(error.message)}</p>`}
}
const studioReady=init();
