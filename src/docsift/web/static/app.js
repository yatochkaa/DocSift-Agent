/* Клиентский минимум: тема, графики, загрузка файлов, копирование JSON, bulk edit.
   Никакой сборки: обычный скрипт с одним глобальным объектом DocSift. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function refreshDocumentsTable() {
    var region = document.getElementById('table-region');
    if (!region || !window.htmx) return;
    window.htmx.ajax('GET', window.location.pathname + window.location.search, {
      target: '#table-region',
      select: '#table-region',
      swap: 'outerHTML'
    });
  }

  var DocSift = {
    /* --- Тема ------------------------------------------------------- */
    toggleTheme: function () {
      var root = document.documentElement;
      var next = root.dataset.theme === 'light' ? 'dark' : 'light';
      root.dataset.theme = next;
      localStorage.setItem('docsift-theme', next);
      DocSift.updateThemeIcon();
      DocSift.renderCharts();
    },

    updateThemeIcon: function () {
      var root = document.documentElement;
      var sunIcon = document.querySelector('.theme-icon-sun');
      var moonIcon = document.querySelector('.theme-icon-moon');
      if (!sunIcon || !moonIcon) return;

      if (root.dataset.theme === 'dark') {
        sunIcon.style.display = 'block';
        moonIcon.style.display = 'none';
      } else {
        sunIcon.style.display = 'none';
        moonIcon.style.display = 'block';
      }
    },

    /* --- Графики Chart.js ---------------------------------------- */
    charts: {},
    renderCharts: function () {
      if (typeof Chart === 'undefined') return;
      var css = getComputedStyle(document.documentElement);
      var text = css.getPropertyValue('--ds-muted').trim();
      var grid = css.getPropertyValue('--ds-border').trim();
      var accent = css.getPropertyValue('--ds-accent').trim();

      document.querySelectorAll('canvas[data-chart]').forEach(function (canvas) {
        var series = JSON.parse(canvas.dataset.series || '{}');
        var type = canvas.dataset.chart;
        if (DocSift.charts[canvas.id]) DocSift.charts[canvas.id].destroy();
        DocSift.charts[canvas.id] = new Chart(canvas, {
          type: type,
          data: {
            labels: series.labels || [],
            datasets: [
              {
                label: series.label || '',
                data: series.values || [],
                borderColor: accent,
                backgroundColor: type === 'bar' ? series.colors || accent : 'transparent',
                borderWidth: type === 'bar' ? 0 : 2,
                borderRadius: 6,
                tension: 0.35,
                pointRadius: 2,
                pointHoverRadius: 4,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: reduceMotion ? false : { duration: 200 },
            plugins: { legend: { display: false } },
            scales: {
              x: { ticks: { color: text }, grid: { display: false, color: grid } },
              y: { ticks: { color: text }, grid: { color: grid }, beginAtZero: true },
            },
          },
        });
      });
    },

    /* --- Загрузка документов ------------------------------------ */
    /* uploadBusy: пока запрос в полёте, окно не закрывается ни по Escape, ни
       по крестику — пользователь должен дождаться ответа сервера. */
    uploadBusy: false,

    openUpload: function () {
      var overlay = document.querySelector('[data-upload-overlay]');
      if (!overlay) return;
      overlay.classList.remove('hidden');
      overlay.classList.add('flex');
      if (typeof DocSift.resetUploadForm === 'function') DocSift.resetUploadForm();
      /* Фокус на input: он визуально скрыт, но фокусируем, и рамку зоны
         подсвечивает #file:focus-visible + .ds-pm-drop. */
      var input = overlay.querySelector('input[type="file"]');
      if (input) input.focus();
    },

    closeUpload: function () {
      if (DocSift.uploadBusy) return;
      var overlay = document.querySelector('[data-upload-overlay]');
      if (!overlay) return;
      overlay.classList.add('hidden');
      overlay.classList.remove('flex');
    },

    /* --- Прокрутка к странице превью ---------------------------- */
    scrollToPage: function (number) {
      var target = document.getElementById('page-' + number);
      if (target) target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
    },

    /* --- Копирование сырого JSON --------------------------------- */
    copy: function (elementId, button) {
      var node = document.getElementById(elementId);
      if (!node) return;
      navigator.clipboard.writeText(node.textContent || '').then(function () {
        var original = button.textContent;
        button.textContent = 'Скопировано';
        setTimeout(function () {
          button.textContent = original;
        }, 1200);
      });
    },

    /* --- Редактирование одного поля по клику ------------------- */
    startFieldEdit: function (card, event) {
      if (!card) return;
      var target = event && event.target;
      if (target && target.closest && target.closest('form')) return;
      document.querySelectorAll('.ds-field.is-editing').forEach(function (other) {
        if (other !== card) other.classList.remove('is-editing');
      });
      card.classList.add('is-editing');
      var form = card.querySelector('.ds-inline-field-form');
      if (form) form.hidden = false;
      var input = card.querySelector('input[name="value"]');
      if (input) { input.focus(); input.select(); }
    },

    cancelFieldEdit: function (button) {
      var card = button && button.closest('.ds-field');
      if (!card) return;
      var input = card.querySelector('input[name="value"]');
      if (input) input.value = input.defaultValue;
      card.classList.remove('is-editing');
      var form = card.querySelector('.ds-inline-field-form');
      if (form) form.hidden = true;
    },

    /* --- Редактирование позиций -------------------------------- */
    toggleEditMode: function () {
      var form = document.getElementById('bulk-edit-form');
      if (!form) return;
      if (form.classList.contains('is-editing')) {
        DocSift.cancelEditMode();
        return;
      }
      form.classList.add('is-editing');
      form.dataset.editMode = 'true';
      var button = document.getElementById('edit-mode-btn');
      if (button) button.innerHTML = '<i data-lucide="x"></i> Закрыть редактирование';
      var first = form.querySelector('.ds-cell-input');
      if (first) first.focus();
      initIcons();
    },

    cancelEditMode: function () {
      var form = document.getElementById('bulk-edit-form');
      if (!form) return;
      form.querySelectorAll('.ds-cell-input').forEach(function (input) {
        input.value = input.defaultValue;
      });
      form.classList.remove('is-editing');
      form.dataset.editMode = 'false';
      var button = document.getElementById('edit-mode-btn');
      if (button) button.innerHTML = '<i data-lucide="edit-3"></i> Редактировать позиции';
      initIcons();
    },

    saveBulkEdit: function () {
      var form = document.getElementById('bulk-edit-form');
      if (!form) return;

      // Collect changed fields
      var changes = [];
      var fieldInputs = form.querySelectorAll('.ds-field-input');
      var cellInputs = form.querySelectorAll('.ds-cell-input');

      for (var i = 0; i < fieldInputs.length; i++) {
        var input = fieldInputs[i];
        var original = input.getAttribute('value');
        var current = input.value;
        var path = input.getAttribute('data-path');
        if (current !== original && path) {
          changes.push({path: path, value: current});
        }
      }

      for (var i = 0; i < cellInputs.length; i++) {
        var input = cellInputs[i];
        var original = input.getAttribute('value');
        var current = input.value;
        var path = input.getAttribute('data-path');
        if (current !== original && path) {
          changes.push({path: path, value: current});
        }
      }

      if (changes.length === 0) {
        DocSift.cancelEditMode();
        return;
      }

      // Submit form
      var formData = new FormData(form);
      for (var i = 0; i < changes.length; i++) {
        formData.append('paths', changes[i].path);
        formData.append('values', changes[i].value);
      }

      fetch(form.action, {
        method: 'POST',
        body: formData
      }).then(function (response) {
        if (response.ok) {
          window.location.href = response.url || (window.location.pathname + '?bulk_saved=1');
        } else {
          return response.text().then(function (text) {
            alert('Ошибка сохранения: ' + text);
          });
        }
      }).catch(function (error) {
        alert('Ошибка сети: ' + error.message);
      });
    },
  };

  window.DocSift = DocSift;

  function initIcons() {
    if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons();
  }

  function initThemeToggle() {
    var button = document.getElementById('theme-toggle-btn');
    if (!button || button.dataset.bound === 'true') return;
    button.dataset.bound = 'true';
    button.addEventListener('click', function () { DocSift.toggleTheme(); });
  }

  /* --- Состояния формы загрузки ----------------------------------
     Скрипт отвечает только за то, что видно: какой шаг формы показан и какой
     текст читает пользователь. Ни лимитов, ни статусов обработки здесь нет —
     их считает сервер и присылает готовой карточкой. */

  function humanSize(bytes) {
    if (typeof bytes !== 'number' || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' Б';
    var units = ['КБ', 'МБ', 'ГБ'];
    var value = bytes;
    for (var i = 0; i < units.length; i++) {
      value = value / 1024;
      if (value < 1024 || i === units.length - 1) {
        return value.toFixed(i === 0 ? 0 : 1).replace('.', ',') + ' ' + units[i];
      }
    }
    return '';
  }

  function humanType(file) {
    var byMime = {
      'application/pdf': 'PDF',
      'image/png': 'PNG',
      'image/jpeg': 'JPEG',
      'image/tiff': 'TIFF'
    }[file.type];
    if (byMime) return byMime;
    var dot = file.name.lastIndexOf('.');
    return dot > -1 ? file.name.slice(dot + 1).toUpperCase() : 'Файл';
  }

  /* Панель показывается ровно тогда, когда в ней есть карточка. */
  function syncTracker() {
    var tracker = document.querySelector('[data-upload-tracker]');
    if (!tracker) return null;
    var card = tracker.querySelector('[data-upload-card]');
    tracker.hidden = !card;
    return card;
  }

  function initUpload() {
    var form = document.getElementById('upload-form');
    var input = document.getElementById('file');
    if (!form || !input) return;

    var zone = form.querySelector('[data-dropzone]');
    var picked = form.querySelector('[data-upload-picked]');
    var pickedName = form.querySelector('[data-picked-name]');
    var pickedMeta = form.querySelector('[data-picked-meta]');
    var submit = form.querySelector('[data-upload-submit]');
    var live = form.querySelector('[data-upload-live]');
    var clearButton = document.querySelector('[data-upload-clear]');
    var replaceButton = document.querySelector('[data-upload-replace]');
    var progress = document.getElementById('upload-progress');

    function say(text) {
      if (!live) return;
      live.textContent = text || '';
      live.hidden = !text;
    }

    function showEmpty() {
      if (zone) zone.hidden = false;
      if (picked) picked.hidden = true;
      if (submit) submit.disabled = true;
    }

    function reset() {
      input.value = '';
      showEmpty();
      say('');
      if (progress) {
        progress.hidden = true;
        progress.value = 0;
      }
    }

    DocSift.resetUploadForm = reset;

    function showPicked() {
      var file = input.files && input.files[0];
      if (!file) {
        reset();
        return;
      }
      if (pickedName) pickedName.textContent = file.name;
      if (pickedMeta) pickedMeta.textContent = humanType(file) + ' · ' + humanSize(file.size);
      if (zone) zone.hidden = true;
      if (picked) picked.hidden = false;
      if (submit) {
        submit.disabled = false;
        submit.focus();
      }
      say('');
    }

    input.addEventListener('change', showPicked);
    if (clearButton) clearButton.addEventListener('click', function () {
      reset();
      input.focus();
    });
    if (replaceButton) replaceButton.addEventListener('click', function () {
      input.click();
    });

    if (zone) {
      ['dragenter', 'dragover'].forEach(function (name) {
        zone.addEventListener(name, function (event) {
          event.preventDefault();
          zone.classList.add('is-over');
        });
      });
      ['dragleave', 'drop'].forEach(function (name) {
        zone.addEventListener(name, function (event) {
          event.preventDefault();
          zone.classList.remove('is-over');
        });
      });
      zone.addEventListener('drop', function (event) {
        if (!event.dataTransfer || !event.dataTransfer.files.length) return;
        input.files = event.dataTransfer.files;
        showPicked();
      });
    }

    /* --- Жизненный цикл запроса --------------------------------- */
    form.addEventListener('htmx:beforeRequest', function () {
      var file = input.files && input.files[0];
      DocSift.uploadBusy = true;
      lastTerminalKey = '';
      say('Загружаем ' + (file ? file.name : 'файл') + '…');
      if (progress) {
        progress.hidden = false;
        progress.value = 0;
      }
    });

    form.addEventListener('htmx:xhr:progress', function (event) {
      if (!progress || !event.detail.lengthComputable) return;
      progress.hidden = false;
      progress.value = (event.detail.loaded / event.detail.total) * 100;
    });

    /* Сеть не ответила вовсе — сервер карточку прислать не смог, объясняем сами. */
    form.addEventListener('htmx:sendError', function () {
      DocSift.uploadBusy = false;
      say('Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.');
      if (progress) progress.hidden = true;
    });

    /* Ответ пришёл (любой) — окно можно закрыть, результат уже в панели. */
    form.addEventListener('htmx:afterRequest', function (event) {
      DocSift.uploadBusy = false;
      if (progress) progress.hidden = true;
      if (!event.detail.xhr) return;
      reset();
      DocSift.closeUpload();
      var card = syncTracker();
      if (card) card.focus();
      if (event.detail.xhr.status < 400) refreshDocumentsTable();
    });
  }

  /* Отказ сервера (413/415/500) htmx по умолчанию не вставляет. Разрешаем
     подмену точечно для панели загрузки: иначе отказ выглядит как молчание.
     Решение чисто визуальное — тело ответа рисует сервер. */
  document.body && document.body.addEventListener('htmx:beforeSwap', function (event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== 'upload-list') return;
    if (event.detail.xhr && event.detail.xhr.status >= 400) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  /* Когда обработка дошла до конца, список обновляем один раз: ключ статуса
     не даёт зациклиться на собственном же htmx:afterSwap от таблицы. */
  var lastTerminalKey = '';

  function syncAfterSwap() {
    var card = syncTracker();
    if (!card) return;
    var key = (card.dataset.status || '') + '|' + (card.dataset.terminal || '');
    if (card.dataset.terminal === 'true' && key !== lastTerminalKey) {
      lastTerminalKey = key;
      refreshDocumentsTable();
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    initIcons();
    initThemeToggle();
    DocSift.updateThemeIcon();
    DocSift.renderCharts();
    initUpload();
    syncTracker();
  });

  /* После HTMX-свопа перерисовываем иконки и графики внутри фрагмента */
  document.body && document.addEventListener('htmx:afterSwap', function () {
    initIcons();
    initThemeToggle();
    DocSift.updateThemeIcon();
    DocSift.renderCharts();
    syncAfterSwap();
  });

  document.addEventListener('keydown', function (event) {
    /* Во время отправки Escape не закрывает окно: ответ сервера ещё не пришёл. */
    if (event.key === 'Escape') DocSift.closeUpload();

    if (event.key === 'Escape') {
      var inline = document.querySelector('.ds-inline-field-form:not([hidden])');
      if (inline) {
        event.preventDefault();
        DocSift.cancelFieldEdit(inline.querySelector('button[type="button"]'));
      }
    }

    /* Edit mode keyboard shortcuts */
    var form = document.getElementById('bulk-edit-form');
    if (form && form.classList.contains('is-editing')) {
      if (event.key === 'Escape') {
        event.preventDefault();
        DocSift.cancelEditMode();
      }
      if (event.ctrlKey && event.key === 'Enter') {
        event.preventDefault();
        DocSift.saveBulkEdit();
      }
    }
  });
})();