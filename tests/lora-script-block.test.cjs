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
