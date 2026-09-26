document.getElementById('pageRefresh')?.addEventListener('click', function () {
  this.disabled = true;
  this.textContent = '새로고침 중…';
  this.setAttribute('aria-busy', 'true');
  window.location.reload();
});
