const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'..','index.html'),'utf8');
const code=source.slice(source.indexOf('function parseLoraBlock('),source.indexOf('function loraBlockSummary('));
const parse=vm.runInNewContext(code+';parseLoraBlock');
const block=`A person walking naturally.
[LORA]
Better Human Motion: 0.60
Insta/TikTok Aesthetics: 0.18
Motion Continuity Repair: 0.92
Spatial Physics: 0.35
Realism People: 1.00
Camera Motion 1000: 0.10
Camera Motion 3000: off
Camera Movement: handheld
[/LORA]`;
test('exact user settings and prompt separation',()=>{const p=parse(block);assert.equal(p.options.insta_tiktok.strength,.18);assert.equal(p.options.motion_repair.strength,.92);assert.equal(p.options.camera_motion.version,'1000');assert.equal(p.options.camera_motion.strength,.1);assert.equal(p.options.camera_motion.movement,'handheld');assert.equal(p.options.realism.strength,1);assert.equal(p.options.spatial_physics.strength,.35);assert.equal(p.options.better_motion.strength,.6);assert.equal(p.prompt,'A person walking naturally.');});
test('off and omitted entries disable LoRAs',()=>{const p=parse('[LORA]\nRealism: OFF\n[/LORA]');assert.equal(p.options.realism.enabled,false);assert.equal(p.options.better_motion.enabled,false);});
for(const [name,text] of Object.entries({missing:'abc',multiple:block+block,badNumber:block.replace('0.18','NaN'),outOfRange:block.replace('0.18','2.1'),unknown:block.replace('Realism People','Unknown'),duplicate:block.replace('Spatial Physics','Realism People'),cameraConflict:block.replace('3000: off','3000: 0.3'),badMovement:block.replace('handheld','orbit')}))test('reject '+name,()=>assert.throws(()=>parse(text)));

async function submitted({checked=true,motion=false,text=block}={}){
 const elements=new Map(),requests=[],alerts=[];
 const $=selector=>{if(!elements.has(selector))elements.set(selector,{value:'',checked:false,style:{},addEventListener(){}});return elements.get(selector);};
 $('#prompt').value=text;$('#scriptLoraAuto').checked=checked;
 const scope={$,MODE:motion?'i2v':'t2v',refKind:motion?'motion':'image',WORKER_TARGET:'pgx',STRATEGY:'single',S:15,SEG_SECONDS:4,W:768,H:1344,STEPS:20,REALISM_LORA:true,REALISM_STRENGTH:.2,CAM_MOTION:'3000',CAM_STRENGTH:.7,refvUploading:false,refvActive:true,refActive:true,imgNonce:'',refvMeta:{sha256:'test'},window:{},alert:m=>alerts.push(m),setProgressCharacter(){},readH3LoraOptions:()=>({realism:{enabled:true,strength:.2}}),H3ReferenceContract:{build:()=>({prompt:'motion contract',negative:'',version:'test',clothing:'video',background:'video'})},fetch:async(url,options)=>{requests.push(JSON.parse(options.body));return {json:async()=>({ok:false,error:'mock end'})};}};
 const start=source.indexOf('function parseLoraBlock('),end=source.indexOf('// 중단',start);
 vm.runInNewContext(source.slice(start,end),scope);
 await $('#go').onclick();return {requests,alerts};
}
test('checked script values override manual values in the actual generation request',async()=>{
 const {requests}=await submitted();assert.equal(requests.length,1);const p=requests[0];
 assert.equal(p.realism_strength,1);assert.equal(p.cam_strength,.1);assert.equal(p.cam_motion,'1000');
 assert.equal(p.lora_options.insta_tiktok.strength,.18);assert.equal(p.prompt,'A person walking naturally.');
});
test('unchecked script preserves manual generation values',async()=>{
 const {requests}=await submitted({checked:false});assert.equal(requests[0].realism_strength,.2);assert.equal(requests[0].cam_motion,'3000');assert.equal(requests[0].prompt,block);
});
test('invalid checked block never submits a generation request',async()=>{
 const {requests,alerts}=await submitted({text:'missing block'});assert.equal(requests.length,0);assert.equal(alerts.length,1);
});
test('motion-reference mode keeps its fixed prompt and LoRA-off contract',async()=>{
 const {requests}=await submitted({motion:true,text:'motion contract'});assert.equal(requests.length,1);assert.equal(requests[0].prompt,'motion contract');assert.equal(Object.keys(requests[0].lora_options).length,0);assert.equal(requests[0].realism_lora,false);
});
