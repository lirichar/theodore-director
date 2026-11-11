const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../web/studio-workflow.js'), 'utf8');
test('legacy history is explicitly selectable, read-only, and truncates long cards', () => {
  assert.ok(source.includes('<option value="legacy">旧版历史</option>'));
  assert.ok(source.includes('历史状态未知'));
  assert.ok(source.includes('完整提示词与参数'));
  const code=definition('legacyPromptSummary');const context=vm.createContext({});vm.runInContext(code,context);
  assert.equal(context.legacyPromptSummary('  a  b  '),'a b');
  assert.equal(context.legacyPromptSummary('x'.repeat(181)).length,181);
});
test('failed tasks expose retry and non-asset views hide inspector', () => {
  assert.ok(source.includes('task-retry'));
  assert.ok(source.includes('task-stalled'));
  assert.ok(source.includes('/api/v2/generation/fail-stalled'));
  assert.ok(source.includes("$('#rerunAdjust').click()"));
  assert.ok(source.includes("classList.toggle('inspector-hidden',!assets)"));
});
test('acceptance navigation is grouped and mobile picker search cannot consume its viewport', () => {
  const html=fs.readFileSync(path.join(__dirname,'../web/studio.html'),'utf8');
  const nav=html.split('<nav id="mainNav"')[1].split('</nav>')[0];
  for(const view of ['video','image','script','assets'])assert.ok(nav.includes(`data-view="${view}"`));
  for(const view of ['characters','scenes','storyboard','qwen_image_edit','character_replace'])assert.ok(!nav.includes(`data-view="${view}"`));
  const css=fs.readFileSync(path.join(__dirname,'../web/acceptance-ui.css'),'utf8');
  assert.match(css,/\.asset-drawer>\.search\{flex:0 0 38px!important/);
  assert.ok(css.includes('grid-auto-rows:max-content'));
  assert.ok(css.includes('object-fit:contain!important'));
  assert.ok(source.includes("limit:'24'"));
});
test('asset management supports explicit multi-select and protected permanent deletion', () => {
  const html=fs.readFileSync(path.join(__dirname,'../web/studio.html'),'utf8');
  const catalogSource=fs.readFileSync(path.join(__dirname,'../web/studio-v2.js'),'utf8');
  assert.ok(html.includes('id="bulkToolbar"'));
  assert.ok(html.includes('id="bulkDelete"'));
  assert.ok(html.includes('id="deleteMedia"'));
  assert.ok(catalogSource.includes('selectedIds:new Set()'));
  assert.ok(catalogSource.includes('/api/v2/media/bulk'));
  assert.ok(catalogSource.includes('/api/v2/media/permanent-delete'));
  assert.ok(catalogSource.includes("runBulkMediaAction('link_project')"));
});
function definition(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, name);
  const end = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, end);
}
const code = source.match(/^const VIDEO_ASPECTS=.*$/m)[0] + '\n' +
  ['videoAspect', 'generationPayload', 'rerunOptions'].map(definition).join('\n');
const context = vm.createContext({});
vm.runInContext(code, context);
const payload = (quick, values) => JSON.parse(JSON.stringify(context.generationPayload(quick, values)));
const values = {title:'测试', prompt:'测试', count:'3', seed:'0', size:'1024x1536', mode:'i2v',
  aspect:'9:16', resolution:'.4', duration:'5', negative:'避免变形', shots:[]};

test('image payload never forwards hidden seed, negative or reference controls', () => {
  const result = payload({kind:'image', mediaIds:['hidden-ref']}, values);
  assert.equal(result.size, '1024x1536');
  assert.equal(result.count, 3);
  assert.deepEqual(result.mediaIds, []);
  for (const key of ['seed','negative','mode','aspect','duration','strength']) assert.equal(key in result, false);
});
test('image edit preserves zero seed, negative prompt and ordered references', () => {
  const result = payload({kind:'image_edit',mediaIds:['b','a']}, values);
  assert.equal(result.seed, 0);
  assert.equal(result.negative, values.negative);
  assert.deepEqual(result.mediaIds, ['b','a']);
  assert.equal('strength' in result, false);
});
test('video payload maps short aspect and explicit first/last roles', () => {
  const result = payload({kind:'video',mediaIds:['first','last']}, values);
  assert.equal(result.aspect, '9:16 (Portrait Widescreen)');
  assert.equal(result.firstFrameId, 'first');
  assert.equal(result.lastFrameId, 'last');
  assert.equal('negative' in result, false);
});
test('text video drops inactive reference choices', () => {
  const result = payload({kind:'video',mediaIds:['old-choice']}, {...values,mode:'t2v'});
  assert.deepEqual(result.mediaIds, []);
  assert.equal('firstFrameId' in result, false);
});
test('adjust rerun carries quantity and complete reproducible context', () => {
  const task = {kind:'image_edit',projectId:'project',shotId:'shot',title:'参考',prompt:'测试',count:5,
    parameters:{seed:0,negative:'不要变形',entityId:'entity',entityType:'character'},
    inputMedia:['second','first'],versions:[{id:'latest'}]};
  const options = JSON.parse(JSON.stringify(context.rerunOptions(task)));
  assert.equal(options.count, 5);
  assert.equal(options.parentVersionId, 'latest');
  assert.equal(options.preset.seed, 0);
  assert.equal(options.preset.negative, '不要变形');
  assert.equal(options.entityId, 'entity');
  assert.deepEqual(options.mediaIds, ['second','first']);
});
test('character replacement payload has distinct role IDs and persists audio choice', () => {
  const result = payload({kind:'video',workflowId:'character_replace',sourceVideoId:'video',characterMediaId:'person',mediaIds:[]}, {...values,startTime:'1.5',preserveAudio:false});
  assert.equal(result.workflowId, 'character_replace');
  assert.equal(result.sourceVideoId, 'video');
  assert.equal(result.characterMediaId, 'person');
  assert.deepEqual(result.mediaIds, ['video','person']);
  assert.equal(result.mode, 'r2v');
  assert.equal(result.startTime, 1.5);
  assert.equal(result.preserveAudio, false);
});
test('character rerun restores workflow and both roles', () => {
  const options = JSON.parse(JSON.stringify(context.rerunOptions({kind:'video',count:2,parameters:{workflowId:'character_replace',sourceVideoId:'v',characterMediaId:'c',preserveAudio:false}})));
  assert.equal(options.workflowId, 'character_replace');
  assert.equal(options.sourceVideoId, 'v');
  assert.equal(options.characterMediaId, 'c');
  assert.equal(options.preset.preserveAudio, false);
});
test('workflow field visibility updates without using a single element as a list', () => {
  const elements = new Map();
  const node = selector => {if(!elements.has(selector))elements.set(selector,{value:'r2v',hidden:false,textContent:'',dataset:{kind:'video',workflow:'character_replace',view:'character_replace'},classList:{toggle(){}},querySelectorAll:()=>[]});return elements.get(selector)};
  const ui = vm.createContext({flow:{quick:{kind:'video',workflowId:'character_replace'}}, $:node,
    $$:selector=>[node(selector)],renderQuickReferences(){},renderReplacementInputs(){},renderWorkflowStatus(){}});
  vm.runInContext(definition('updateQuickFields'), ui);
  ui.updateQuickFields();
  assert.equal(node('.ordinary-video-field').hidden, true);
  assert.equal(node('#characterFields').hidden, false);
  assert.equal(node('#referenceSection').hidden, true);
  assert.equal(node('#headTitle').textContent, '视频编辑与生成');
  ui.flow.quick = {kind:'image_edit',workflowId:''};
  ui.updateQuickFields();
  assert.equal(node('#characterFields').hidden, true);
  assert.equal(node('#referenceSection').hidden, false);
});
