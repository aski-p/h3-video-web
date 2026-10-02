'use strict';

const {execFileSync}=require('node:child_process');

const runtimeFiles=new Set([
  'vercel.json','vercel-ignore-build.cjs','build.sh','index.html',
  'backend_proxy.py','media_ticket.py','studio_reference.py',
  'requirements.txt','.python-version','apple-redesign.css','reference-contract.js'
]);

function needsBuild(files){
  return !files.length||files.some(file=>runtimeFiles.has(file)||file.startsWith('assets/'));
}

function main(){
  const base=process.env.VERCEL_GIT_PREVIOUS_SHA||'HEAD^';
  try{
    execFileSync('git',['cat-file','-e',`${base}^{commit}`],{stdio:'ignore'});
    const files=execFileSync('git',['diff','--no-renames','--name-only',base,'HEAD','--'],{encoding:'utf8'}).trim().split('\n').filter(Boolean);
    const build=needsBuild(files);
    console.log(build?'H3 Vercel source changed: build':'H3 runtime-only or documentation change: skip Vercel build');
    return build?1:0;
  }catch{
    console.log('No comparable Git history: build');
    return 1;
  }
}

if(require.main===module)process.exitCode=main();
module.exports={needsBuild};
