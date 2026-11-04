/* crop-confirm.js —— 裁边确认页（Phase 2-c）。
 *
 * 只做三件事，不引入任何框架：
 *   1. 读 GET /api/scans/{id}/crop-info（只读 sidecar，不重跑检测），把四角画出来；
 *   2. 按住角上的圆点拖动，坐标按 img.naturalWidth / rect.width 换回原图坐标；
 *   3. POST /scans/{id}/crop：{"corners": [...]} 或 {"skip": true}，成功后去挑题页；
 *      失败（含 400 的校验不过）把 detail 写在页面上，绝不静默。
 *
 * 角点自己存的是**原图坐标**，屏幕位置只是按百分比投影——所以窗口缩放不用重算坐标，
 * 只是把线和点再画一遍。
 */
(function () {
  'use strict';

  var stage = document.getElementById('crop-stage');
  if (!stage) { return; }

  var scanId = stage.getAttribute('data-scan-id');
  var english = stage.getAttribute('data-lang') === 'en';
  var modified = false;
  function tr(zh, en) { return english ? en : zh; }
  function withLanguage(url) {
    if (!english) { return url; }
    var target = new URL(url, window.location.href);
    target.searchParams.set('lang', 'en');
    return target.pathname + target.search + target.hash;
  }
  var languageLink = document.getElementById('workflow-language');
  if (languageLink) {
    languageLink.addEventListener('click', function (event) {
      if (modified && !window.confirm(tr('切换语言会丢失尚未保存的裁边调整，继续吗？',
          'Changing language reloads the page and discards your unsaved crop adjustments. Continue?'))) {
        event.preventDefault();
      }
    });
  }
  var img = document.getElementById('crop-image');
  var canvas = document.getElementById('crop-canvas');
  var statusEl = document.getElementById('crop-status');
  var applyBtn = document.getElementById('crop-apply');
  var skipBtn = document.getElementById('crop-skip');
  var redetectBtn = document.getElementById('crop-redetect');

  var handles = [];
  for (var index = 0; index < 4; index += 1) {
    var node = document.getElementById('crop-handle-' + index);
    if (node) { handles.push(node); }
  }

  var corners = null;   // [[x, y] × 4]，原图坐标，顺时针 tl → tr → br → bl
  var size = [0, 0];    // 原图宽高
  var dragIndex = -1;   // 正在拖第几个角；-1 = 没在拖
  var dragPointer = null;

  // ------------------------------------------------------------ 网址

  function infoUrl() { return '/api/scans/' + scanId + '/crop-info'; }
  function cropUrl() { return '/scans/' + scanId + '/crop'; }
  function questionsUrl() { return '/scans/' + scanId + '/questions'; }

  // ------------------------------------------------------------ 页面上的一句话

  function say(text, kind) {
    if (!statusEl) { return; }
    statusEl.textContent = text || '';
    statusEl.className = 'notice notice--' + (kind || 'info');
    statusEl.hidden = !text;
  }

  function complain(error) {
    var message = error && error.message ? error.message : '';
    say(message || tr('没成功，再试一次。', 'Something went wrong. Please try again.'), 'danger');
  }

  // ------------------------------------------------------------ 坐标

  function toImage(clientX, clientY) {
    var rect = img.getBoundingClientRect();
    var naturalWidth = img.naturalWidth || size[0] || 1;
    var naturalHeight = img.naturalHeight || size[1] || 1;
    var x = (clientX - rect.left) * (naturalWidth / (rect.width || 1));
    var y = (clientY - rect.top) * (naturalHeight / (rect.height || 1));
    return [
      Math.min(Math.max(x, 0), naturalWidth),
      Math.min(Math.max(y, 0), naturalHeight)
    ];
  }

  function defaultCorners(width, height) {
    var insetX = Math.round(width * 0.05);
    var insetY = Math.round(height * 0.05);
    return [
      [insetX, insetY],
      [width - insetX, insetY],
      [width - insetX, height - insetY],
      [insetX, height - insetY]
    ];
  }

  // ------------------------------------------------------------ 画

  function place() {
    if (!corners || !size[0] || !size[1]) { return; }
    for (var i = 0; i < handles.length; i += 1) {
      // 位置用百分比：换行/转屏之后不用重算，只是把线重画一遍
      handles[i].style.left = (corners[i][0] / size[0] * 100) + '%';
      handles[i].style.top = (corners[i][1] / size[1] * 100) + '%';
    }
  }

  function token(name) {
    var value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || '').trim();
  }

  // 四个角往纸里收 6px 画 L 形角标：两条臂各 --crop-bracket-size 长、3px 粗。
  // 角标的朝向由角的序号决定（0 左上往右下、1 右上往左下，依此类推）。
  var INWARD = [[1, 1], [-1, 1], [-1, -1], [1, -1]];

  function brackets(context, points) {
    var accent = token('--color-accent');
    var arm = parseFloat(token('--crop-bracket-size')) || 24;
    var inset = 6;
    if (!accent) { return; }
    context.strokeStyle = accent;
    context.lineWidth = 3;
    points.forEach(function (point, i) {
      var sx = INWARD[i][0];
      var sy = INWARD[i][1];
      var x = point[0] + inset * sx;
      var y = point[1] + inset * sy;
      context.beginPath();
      context.moveTo(x, y);
      context.lineTo(x + arm * sx, y);
      context.moveTo(x, y);
      context.lineTo(x, y + arm * sy);
      context.stroke();
    });
  }

  function draw() {
    if (!canvas || !corners || !size[0] || !size[1]) { return; }
    // 用图片自己的盒子：canvas 就铺在图片上，按 stage 量会多出那 1px 边框
    var rect = img.getBoundingClientRect();
    var width = Math.round(rect.width);
    var height = Math.round(rect.height);
    if (!width || !height) { return; }

    var ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);

    var context = canvas.getContext('2d');
    if (!context) { return; }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);

    var points = corners.map(function (point) {
      return [point[0] / size[0] * width, point[1] / size[1] * height];
    });

    function trace() {
      context.beginPath();
      points.forEach(function (point, i) {
        if (i === 0) { context.moveTo(point[0], point[1]); }
        else { context.lineTo(point[0], point[1]); }
      });
      context.closePath();
    }

    // 一、整幅铺一层遮罩：框外暗下去
    var scrim = token('--crop-scrim');
    if (scrim) {
      context.fillStyle = scrim;
      context.fillRect(0, 0, width, height);
    }

    // 二、把四边形里的遮罩再挖掉：纸保持原亮度（不画白，只是不留暗）
    context.globalCompositeOperation = 'destination-out';
    trace();
    context.fill();
    context.globalCompositeOperation = 'source-over';

    // 三、框线两层：深底 3px + 白线 2px，红底和白纸上都看得见
    var halo = token('--crop-edge-halo');
    var inner = token('--crop-edge-inner');
    context.lineJoin = 'round';
    if (halo) {
      context.strokeStyle = halo;
      context.lineWidth = 3;
      trace();
      context.stroke();
    }
    if (inner) {
      context.strokeStyle = inner;
      context.lineWidth = 2;
      trace();
      context.stroke();
    }

    // 四、四角的 L 形角标（视觉上的四支角；拖拽热区仍是 CSS 的 44px 圆点）
    brackets(context, points);
  }

  function redraw() {
    place();
    draw();
  }

  // ------------------------------------------------------------ 拖角

  function pointOf(event) {
    if (!corners) { return; }
    modified = true;
    var point = toImage(event.clientX, event.clientY);
    corners[dragIndex] = [Math.round(point[0]), Math.round(point[1])];
    redraw();
  }

  function onPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) { return; }
    var corner = parseInt(this.getAttribute('data-corner'), 10);
    if (!(corner >= 0)) { return; }
    dragIndex = corner;
    dragPointer = event.pointerId;
    if (this.setPointerCapture) { this.setPointerCapture(event.pointerId); }
    this.classList.add('crop-handle--active');
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (dragIndex < 0 || event.pointerId !== dragPointer) { return; }
    pointOf(event);
    event.preventDefault();
  }

  function onPointerUp(event) {
    if (event.pointerId !== dragPointer) { return; }
    dragIndex = -1;
    dragPointer = null;
    this.classList.remove('crop-handle--active');
  }

  handles.forEach(function (handle) {
    handle.addEventListener('pointerdown', onPointerDown);
    handle.addEventListener('pointermove', onPointerMove);
    handle.addEventListener('pointerup', onPointerUp);
    handle.addEventListener('pointercancel', onPointerUp);
  });

  // ------------------------------------------------------------ 读结论

  function adopt(payload) {
    var width = Number(payload && payload.width) || img.naturalWidth || 0;
    var height = Number(payload && payload.height) || img.naturalHeight || 0;
    size = [width, height];

    var detected = payload && payload.corners;
    if (detected && detected.length === 4) {
      corners = detected.map(function (point) {
        return [Number(point[0]), Number(point[1])];
      });
    } else if (!corners && width && height) {
      // 老卷子没有 sidecar：给一个"向内收 5%"的起点，家长自己拖
      corners = defaultCorners(width, height);
    }
    redraw();
  }

  function load() {
    return fetch(infoUrl(), { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(tr('读不到裁切结论（' + response.status + '）', 'Could not load the crop details (' + response.status + ').'));
        }
        return response.json();
      })
      .then(adopt);
  }

  // ------------------------------------------------------------ 提交

  function send(body, done) {
    say(tr('正在提交…', 'Saving…'), 'info');
    return fetch(cropUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (response) {
        return response.text().then(function (text) {
          var payload = null;
          try { payload = text ? JSON.parse(text) : null; } catch (err) { payload = null; }
          if (!response.ok) {
            var detail = payload && payload.detail;
            throw new Error(
              english ? (response.status === 400 ? 'Check that the four corners outline a large, valid page.' : 'Could not save. Refresh the page and try again (' + response.status + ').') :
                (typeof detail === 'string' && detail ? detail : '提交失败（' + response.status + '）')
            );
          }
          return payload;
        });
      })
      .then(function (payload) {
        say(done, 'info');
        window.location.href = withLanguage(payload && payload.next_url ? payload.next_url : questionsUrl());
      })
      .catch(complain);
  }

  if (applyBtn) {
    applyBtn.addEventListener('click', function () {
      if (!corners) {
        say(tr('还没拿到四个角，等一下再点。', 'The corners are still loading. Please wait.'), 'danger');
        return;
      }
      send({
        corners: corners.map(function (point) {
          return [Math.round(point[0]), Math.round(point[1])];
        })
      }, tr('四个角记下了，正要去圈错题…', 'Corners saved. Opening question selection…'));
    });
  }

  if (skipBtn) {
    // 「跳过」= 家长不动，接口只标 human="skipped"，不重裁、不出新图
    skipBtn.addEventListener('click', function () {
      send({ skip: true }, tr('那就用现在这张，正要去挑题…', 'Using this image. Opening question selection…'));
    });
  }

  if (redetectBtn) {
    redetectBtn.addEventListener('click', function () {
      // 只重新读 crop-info，不调任何检测接口
      load()
        .then(function () { modified = false; say(tr('已经把这张照片记下的裁切结论重读了一遍。', 'Crop details reloaded.'), 'info'); })
        .catch(complain);
    });
  }

  // ------------------------------------------------------------ 起跑

  window.addEventListener('resize', redraw);

  var start = function () {
    if (!size[0]) { size = [img.naturalWidth || 0, img.naturalHeight || 0]; }
    load().catch(function (error) {
      if (!corners && size[0] && size[1]) {
        corners = defaultCorners(size[0], size[1]);
        redraw();
      }
      complain(error);
    });
  };

  if (img.complete && img.naturalWidth) { start(); }
  else { img.addEventListener('load', start); }
  img.addEventListener('error', function () {
    say(tr('这张原图读不出来，可以先点「跳过」。', 'The original image could not be loaded. You can try skipping the crop.'), 'danger');
  });
})();
