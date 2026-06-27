const CONFIG = {
  APP_NAME: 'Epoch',
  USER_DISPLAY_NAME: 'Mars',
  APP_THEME: '#4A1C6B',
  VERSION: '11.0'
};

// ── Bloque A (seguridad): la llave de acceso viaja en toda escritura ─────────
// Se guarda UNA vez en Perfil → localStorage. Nunca vive en el código fuente.
(function(){
  const _fetch=window.fetch;
  window.fetch=function(url,opts){
    opts=opts||{};
    const m=(opts.method||'GET').toUpperCase();
    if(m!=='GET'&&m!=='HEAD'){
      const k=localStorage.getItem('epoch_key');
      if(k){opts.headers=Object.assign({},opts.headers,{'X-Epoch-Key':k});}
    }
    return _fetch(url,opts).then(function(r){
      if(r.status===401&&typeof toast==='function')toast('Access key needed — set it in Profile');
      return r;
    });
  };
})();
