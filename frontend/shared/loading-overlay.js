(function(){
  var MIN_MS = 350, FAILSAFE_MS = 6000, start = performance.now(), done = false;
  function hide(){
    if(done) return; done = true;
    var el = document.getElementById('load-overlay');
    if(!el) return;
    el.classList.add('lo-hide');
    setTimeout(function(){ el.remove(); }, 300);
  }
  function hideAfterMin(){
    var elapsed = performance.now() - start;
    setTimeout(hide, Math.max(0, MIN_MS - elapsed));
  }
  window.addEventListener('load', hideAfterMin);
  setTimeout(hide, FAILSAFE_MS);
})();
