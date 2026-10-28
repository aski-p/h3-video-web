(()=>{
 'use strict';
 const start=async()=>{
  const gate=document.getElementById('studioLoginGate'),status=document.getElementById('studioLoginStatus'),host=document.getElementById('studioAccount'),form=document.getElementById('studioPasswordForm');
  if(!gate||!form)return;
  const show=user=>{gate.remove();host.hidden=false;const label=document.createElement('span');label.textContent=`${user.name} · ${user.email}`;host.append(label);const button=document.createElement('button');button.type='button';button.textContent='로그아웃';button.onclick=async()=>{button.disabled=true;const r=await fetch('/api/auth/logout',{method:'POST'});if(r.ok)location.reload();else button.disabled=false;};host.append(button);};
  form.addEventListener('submit',async e=>{e.preventDefault();const input=document.getElementById('studioPassword'),button=form.querySelector('button');button.disabled=true;try{const r=await fetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:input.value})});input.value='';const d=await r.json();if(!r.ok||!d.user){status.textContent=d.error||'비밀번호를 확인해 주세요.';return;}show(d.user);}catch{status.textContent='로그인 서버에 연결하지 못했어요. 다시 시도해 주세요.';}finally{button.disabled=false;}});
  try{const r=await fetch('/api/auth/me',{cache:'no-store',signal:AbortSignal.timeout(15000)}),d=await r.json();if(r.ok&&d.user)show(d.user);}catch{status.textContent='로그인 상태를 확인할 수 없어요. 비밀번호로 다시 로그인해 주세요.';}
 };
 document.addEventListener('DOMContentLoaded',start,{once:true});
})();
