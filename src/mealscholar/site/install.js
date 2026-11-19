// Add to Home Screen.
// Chrome (Android and desktop): the browser's own install prompt, caught from beforeinstallprompt.
// Safari on iPhone and iPad: there is no prompt, so a sheet shows the Share > Add to Home Screen steps.
// [data-install] buttons stay hidden until one of those can work, and never show once the app runs
// from the Home Screen. ?install=1 (the landing page's button) opens the sheet on load on phones.
// The sheet is a <dialog> opened with showModal(), like the app's other sheets: it sits above them
// (Settings included), the page behind it is inert, Escape closes it, and focus goes back
// to the button that opened it.
(function () {
  var d = document, w = window;
  var standalone = w.matchMedia('(display-mode: standalone)').matches || w.navigator.standalone === true;
  var ua = w.navigator.userAgent || '';
  var iPad = /iPad/.test(ua) || (/Macintosh/.test(ua) && w.navigator.maxTouchPoints > 1);
  var iOS = iPad || /iPhone|iPod/.test(ua);
  // Safari itself can add to the Home Screen; other browsers and in-app views on iOS may not.
  var iosSafari = iOS && /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|GSA\/|DuckDuckGo|YaBrowser|FBAN|FBAV|Instagram|Line\//.test(ua);
  var reduced = w.matchMedia('(prefers-reduced-motion: reduce)');
  var bottomSheet = w.matchMedia('(max-width: 39.99em)');
  var deferred = null; // the browser's install prompt, while there is one to show
  var offered = false; // the browser has offered an install: it can install, prompt or not
  var installed = false;
  var opener = null;
  var closeTimer = 0;

  function available() {
    return !standalone && !installed && (offered || iosSafari);
  }

  function sync() {
    d.querySelectorAll('[data-install]').forEach(function (b) { b.hidden = !available(); });
    var now = d.querySelector('#install-sheet [data-install-now]');
    if (now) now.hidden = !deferred;
  }

  w.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferred = e;
    offered = true;
    sync();
  });
  w.addEventListener('appinstalled', function () {
    deferred = null;
    installed = true;
    sync();
    hide(true);
  });

  var share = '<svg class="install__glyph" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5v11M8 7.5l4-4 4 4M7 11H6a1.5 1.5 0 0 0-1.5 1.5v7A1.5 1.5 0 0 0 6 21h12a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 18 11h-1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var steps = iOS
    ? ['Tap the <b>Share</b> button ' + share + ' at the ' + (iPad ? 'top' : 'bottom') + ' of Safari.',
       'Scroll down and tap <b>Add to Home Screen</b>.',
       'Tap <b>Add</b>. MealScholar appears with your other apps.']
    : ['Open the browser <b>menu</b> (the three dots).',
       'Tap <b>Add to Home screen</b> or <b>Install app</b>.',
       'Tap <b>Add</b>. MealScholar appears with your other apps.'];

  // A 12-second screen recording of the three steps in Safari. Muted and looping; with reduced
  // motion it waits on its poster with controls instead of playing by itself.
  function video() {
    var still = w.matchMedia && w.matchMedia('(prefers-reduced-motion: reduce)').matches;
    return '<div class="install__video">' +
      '<video src="media/add-to-home-screen.mp4" poster="media/add-to-home-screen.jpg" width="624" height="780" muted loop playsinline preload="metadata"' +
      (still ? ' controls' : ' autoplay') +
      ' aria-label="Screen recording: in Safari, Share, then Add to Home Screen, then Add, and MealScholar appears on the Home Screen"></video>' +
      '</div>';
  }

  function sheet() {
    var el = d.getElementById('install-sheet');
    if (el) return el;
    el = d.createElement('dialog');
    el.id = 'install-sheet';
    el.className = 'sheet sheet--install';
    el.setAttribute('aria-labelledby', 'install-title');
    el.setAttribute('aria-describedby', 'install-lede');
    el.innerHTML =
      '<div class="sheet__inner">' +
        '<div class="sheet__grab" aria-hidden="true"><span></span></div>' +
        '<div class="sheet__head">' +
          '<div class="install__head">' +
            '<img class="install__icon" src="icons/icon-192.png" alt="" width="56" height="56">' +
            '<div><h2 id="install-title" class="install__title" tabindex="-1">Add MealScholar to your Home Screen</h2>' +
            '<p id="install-lede" class="install__lede">Opens like an app, with no app store download.</p></div>' +
          '</div>' +
        '</div>' +
        '<div class="sheet__body">' +
          (iOS ? video() : '') +
          '<ol class="install__steps">' + steps.map(function (s, i) {
            return '<li><span class="install__num" aria-hidden="true">' + (i + 1) + '</span><span>' + s + '</span></li>';
          }).join('') + '</ol>' +
          '<div class="install__actions">' +
            '<button type="button" class="btn btn--primary btn--block" data-install-now hidden>Install now</button>' +
            '<button type="button" class="btn btn--secondary btn--block" data-install-close>Not now</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    d.body.appendChild(el);

    var pressedBackdrop = false;
    el.addEventListener('pointerdown', function (e) { pressedBackdrop = e.target === el; });
    el.addEventListener('click', function (e) {
      if (e.target === el && pressedBackdrop) return hide(); // a tap on the dimmed page
      if (e.target.closest('[data-install-close]')) hide();
      else if (e.target.closest('[data-install-now]')) promptNow(opener);
    });
    // Escape and the Android back gesture: animate out like the other sheets.
    el.addEventListener('cancel', function (e) { e.preventDefault(); hide(); });
    el.addEventListener('close', function () {
      clearTimeout(closeTimer);
      el.classList.remove('is-closing');
      el.style.transition = '';
      el.style.transform = '';
      d.documentElement.classList.remove('install-open');
      var back = opener;
      opener = null;
      var usable = back && back.isConnected && !back.hidden && back.getClientRects().length;
      // The button that opened the sheet, else the heading of the sheet it sat in, else the page.
      var host = back && back.isConnected && back.closest && back.closest('dialog[open]');
      var target = usable ? back : host ? host.querySelector('[tabindex="-1"]') : d.getElementById('main');
      if (target && d.activeElement !== target) target.focus({ preventScroll: true });
    });
    draggable(el);
    return el;
  }

  /** Drag the sheet down by its grabber or header to dismiss it, as with the app's sheets. */
  function draggable(el) {
    var drag = null;
    el.addEventListener('pointerdown', function (e) {
      if (!bottomSheet.matches || e.button !== 0 || e.target.closest('button')) return;
      if (!e.target.closest('.sheet__grab, .sheet__head')) return;
      drag = { id: e.pointerId, y: e.clientY, dy: 0 };
      el.setPointerCapture(e.pointerId);
      el.style.transition = 'none';
    });
    el.addEventListener('pointermove', function (e) {
      if (!drag || drag.id !== e.pointerId) return;
      drag.dy = Math.max(0, e.clientY - drag.y);
      el.style.transform = 'translateY(' + drag.dy + 'px)';
    });
    function end(e) {
      if (!drag || drag.id !== e.pointerId) return;
      var dy = drag.dy;
      drag = null;
      if (dy > 80) {
        el.style.transition = 'transform 200ms ease-in';
        el.style.transform = 'translateY(100%)';
        clearTimeout(closeTimer);
        closeTimer = w.setTimeout(function () { if (el.open) el.close(); }, 190);
        return;
      }
      el.style.transition = 'transform 240ms cubic-bezier(0.16, 1, 0.3, 1)';
      el.style.transform = '';
    }
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
  }

  /** `from`: the button that opened the sheet. Safari doesn't focus a tapped button, so it's passed in. */
  function show(from) {
    if (standalone || installed) return;
    var el = sheet();
    sync();
    clearTimeout(closeTimer);
    el.classList.remove('is-closing');
    el.style.transform = '';
    if (el.open) return;
    opener = from || d.activeElement;
    d.documentElement.classList.add('install-open');
    el.showModal();
    el.querySelector('#install-title').focus();
  }

  function hide(now) {
    var el = d.getElementById('install-sheet');
    if (!el || !el.open) return;
    clearTimeout(closeTimer);
    if (now || reduced.matches) return el.close();
    el.classList.add('is-closing');
    closeTimer = w.setTimeout(function () { if (el.open) el.close(); }, 190);
  }

  /** The browser's own prompt. It needs a tap, so it runs from a button, never on page load. */
  function promptNow(from) {
    var p = deferred;
    if (!p) return show(from);
    var shown;
    try {
      shown = p.prompt();
    } catch (err) {
      return show(from); // the browser said no: the steps still work
    }
    deferred = null; // each prompt can be shown once
    sync();
    hide(true);
    Promise.resolve(shown).then(function () { return p.userChoice; }).catch(function () { show(from); });
  }

  /** From an Add to Home Screen button: the browser's prompt while it has one, else the steps
      (after a dismissed prompt, the browser menu still installs). */
  function open(from) {
    if (standalone || installed) return;
    if (deferred && !iOS) return promptNow(from);
    show(from);
  }

  d.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-install]');
    if (!b) return;
    e.preventDefault();
    open(b);
  });

  sync();
  // From the landing page (?install=1), on phones only: Safari on an iPhone or iPad, and Android. No
  // tap has happened on this page yet, so this shows the steps; "Install now" appears in them as soon
  // as the browser offers a prompt. Laptops and in-app browsers just open the app.
  if (w.__wantInstall && !standalone && (iosSafari || /Android/.test(ua))) w.setTimeout(show, 350);
  w.MealScholarInstall = { open: open, close: hide, available: available };
})();
