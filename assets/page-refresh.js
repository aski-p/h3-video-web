document.getElementById('pageRefresh')?.addEventListener('click', function () {
  this.disabled = true;
  this.setAttribute('aria-busy', 'true');
  this.setAttribute('aria-label', '페이지 새로고침 중');
  requestAnimationFrame(() => requestAnimationFrame(() => window.location.reload()));
});
