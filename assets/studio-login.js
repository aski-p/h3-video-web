(()=>{
 'use strict';
 const start=async()=>{
  const gate=document.getElementById('studioLoginGate'),status=document.getElementById('studioLoginStatus'),host=document.getElementById('studioAccount');
  try{
   const r=await fetch('/api/auth/me',{cache:'no-store'}),d=await r.json();
   if(!r.ok||!d.user){status.textContent=r.status===403?'Aski Studio 가입·승인 후 이용할 수 있어요.':'Aski Studio Google 계정으로 로그인해 주세요.';gate.querySelector('a').hidden=false;return;}
   gate.remove();host.hidden=false;const label=document.createElement('span');label.textContent=`${d.user.name} · ${d.user.email}`;host.append(label);
   const button=document.createElement('button');button.type='button';button.textContent='로그아웃';button.onclick=async()=>{button.disabled=true;const response=await fetch('/api/auth/logout',{method:'POST'});if(response.ok)location.reload();else button.disabled=false;};host.append(button);
  }catch{status.textContent='로그인 상태를 확인할 수 없어요. 잠시 후 다시 시도해 주세요.';gate.querySelector('a').hidden=false;}
 };
 document.addEventListener('DOMContentLoaded',start,{once:true});
})();
