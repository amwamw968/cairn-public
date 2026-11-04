/* select-questions.js —— 圈选入库页（Phase 3-a / 3-b）。
 *
 * 只做这几件事，不引入任何框架：
 *   1. 把模板里带 data-x0..y1（**原图坐标**）的自动框按百分比摆到图上；
 *   2. 点框 = 勾上/取消（默认一个都不勾）；
 *   3. 框右上角的 × 删掉这个框（删了就既不显示也不入库，但**要记进反馈**：
 *      "机器这儿放了个框，人把它删了"正是最有价值的标注）；
 *   4. 空白处单指拖动 = 画一个新的人工框，松手就默认勾上；
 *   5. 「确认入库」把勾中的自动框发给 POST /scans/{id}/questions、人工框发给
 *      POST /scans/{id}/frame，两条都成功之后再把这一页的四个动作
 *      （accept / delete / add）POST 给 /api/cut-feedback，最后才跳 /scans/{id}。
 *
 * 顺序不能反：反馈要说"选了 2 个"，那 2 个就必须真的入库了。所以反馈放在最后，
 * 入库任一条失败就一个字都不发（docs/CUT-FEEDBACK-SPEC.md「一页只提交一次快照」）。
 *
 * 坐标一律存**原图坐标**：屏幕 → 原图用 crop-confirm.js 那条式子
 * （naturalWidth / rect.width），摆位置时再换回百分比。窗口缩放不用重算坐标，
 * 百分比自己跟着走。
 */
(function () {
  'use strict';

  var stage = document.getElementById('select-stage');
  if (!stage) { return; }

  var scanId = stage.getAttribute('data-scan-id');
  var workflowMode = stage.getAttribute('data-workflow') === '1';
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
      if (modified && !window.confirm(tr('切换语言会丢失尚未保存的圈选，继续吗？',
          'Changing language reloads the page and discards your unsaved selections. Continue?'))) {
        event.preventDefault();
      }
    });
    ['workflow-subject', 'workflow-question-type'].forEach(function (id) {
      var input = document.getElementById(id);
      if (input) { input.addEventListener('input', function () { modified = true; }); }
    });
  }
  var img = document.getElementById('select-image');
  var layer = document.getElementById('select-boxes');
  var statusEl = document.getElementById('select-status');
  var submitBtn = document.getElementById('select-submit');
  var allBtn = document.getElementById('select-all');
  var noneBtn = document.getElementById('select-none');
  var countAuto = document.getElementById('count-auto');
  var countManual = document.getElementById('count-manual');
  var countPicked = document.getElementById('count-picked');
  var submitCount = document.getElementById('submit-count');

  // 与后端 scan_service.MANUAL_MIN_FRAME_SIDE 同一个数：太小的框后端整批退回，
  // 这里先挡下，免得家长画一下手抖就丢一次提交。
  var MIN_SIDE = 30;

  // 反馈的元数据由服务端渲染好（页面自己算不出文件指纹和配方）：
  // 底图 sha256、bbox 所在坐标系的宽高、配方名、切块参数指纹。
  var meta = {
    sha256: stage.getAttribute('data-image-sha256') || '',
    width: Number(stage.getAttribute('data-image-w')) || 0,
    height: Number(stage.getAttribute('data-image-h')) || 0,
    recipe: stage.getAttribute('data-recipe') || '',
    paramsHash: stage.getAttribute('data-params-hash') || ''
  };

  var boxes = [];     // {el, key, source: 'auto'|'manual', strategy, no, x0, y0, x1, y1, on}
  var dropped = [];   // 被 × 删掉的自动框：页面上没了，但反馈里要留一条 delete
  var drag = null;    // {pointerId, x0, y0, ghost}
  var sending = false;
  // 两条入库都成功了 = true。之后再点提交只补发反馈，绝不重复 POST /frame
  // （人工框第二次提交会在库里再建一遍题）。
  var committed = false;
  var workflowNextUrl = '/scans/' + scanId;
  var manualSeq = 0;

  // ------------------------------------------------------------ 网址

  function questionsUrl() { return '/scans/' + scanId + '/questions'; }
  function frameUrl() { return '/scans/' + scanId + '/frame'; }
  var FEEDBACK_URL = '/api/cut-feedback';

  // ------------------------------------------------------------ 页面上的一句话

  function say(text, kind) {
    if (!statusEl) { return; }
    statusEl.textContent = text || '';
    statusEl.className = 'notice notice--' + (kind || 'info');
    statusEl.hidden = !text;
  }

  // ------------------------------------------------------------ 坐标

  function size() {
    return [img.naturalWidth || 0, img.naturalHeight || 0];
  }

  // 屏幕点 → 原图坐标（crop-confirm.js 的 toImage 同一条式子）
  function toImage(clientX, clientY) {
    var rect = img.getBoundingClientRect();
    var natural = size();
    var x = (clientX - rect.left) * (natural[0] / (rect.width || 1));
    var y = (clientY - rect.top) * (natural[1] / (rect.height || 1));
    return [
      Math.min(Math.max(x, 0), natural[0]),
      Math.min(Math.max(y, 0), natural[1])
    ];
  }

  // 原图坐标 → 图上的百分比（框与图片同一个盒子，边框不吃这层百分比）
  function paint(el, box) {
    var natural = size();
    if (!natural[0] || !natural[1]) { return; }
    el.style.left = (box.x0 / natural[0] * 100) + '%';
    el.style.top = (box.y0 / natural[1] * 100) + '%';
    el.style.width = ((box.x1 - box.x0) / natural[0] * 100) + '%';
    el.style.height = ((box.y1 - box.y0) / natural[1] * 100) + '%';
  }

  function normalize(ax, ay, bx, by) {
    return [
      Math.round(Math.min(ax, bx)),
      Math.round(Math.min(ay, by)),
      Math.round(Math.max(ax, bx)),
      Math.round(Math.max(ay, by))
    ];
  }

  // ------------------------------------------------------------ 框

  function adopt(el) {
    var box = {
      el: el,
      key: el.getAttribute('data-frame-key') || ('a' + el.getAttribute('data-no')),
      source: el.getAttribute('data-source') === 'manual' ? 'manual' : 'auto',
      strategy: el.getAttribute('data-strategy') || 'anchor_digit',
      no: el.getAttribute('data-no') === null ? null : parseInt(el.getAttribute('data-no'), 10),
      x0: Number(el.getAttribute('data-x0')) || 0,
      y0: Number(el.getAttribute('data-y0')) || 0,
      x1: Number(el.getAttribute('data-x1')) || 0,
      y1: Number(el.getAttribute('data-y1')) || 0,
      on: el.getAttribute('data-selected') === '1'
    };
    boxes.push(box);
    el.classList.toggle('select-box--on', box.on);
    paint(el, box);
    return box;
  }

  function make(rect) {
    var el = document.createElement('div');
    el.className = 'select-box';
    el.setAttribute('data-source', 'manual');

    var label = document.createElement('span');
    label.className = 'select-box__no';
    label.textContent = tr('人工', 'Manual');

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'select-box__x';
    close.setAttribute('aria-label', tr('删掉这个人工框', 'Remove this manual box'));

    el.appendChild(label);
    el.appendChild(close);
    layer.appendChild(el);
    return el;
  }

  function find(el) {
    for (var i = 0; i < boxes.length; i += 1) {
      if (boxes[i].el === el) { return boxes[i]; }
    }
    return null;
  }

  function toggle(box) {
    if (!box) { return; }
    modified = true;
    box.on = !box.on;
    box.el.classList.toggle('select-box--on', box.on);
    render();
  }

  function drop(box) {
    if (!box) { return; }
    modified = true;
    var index = boxes.indexOf(box);
    if (index >= 0) { boxes.splice(index, 1); }
    if (box.el.parentNode) { box.el.parentNode.removeChild(box.el); }
    if (box.source === 'auto') {
      // 自动框被删 = 机器在那儿放错了一个框，这是要记下来的一条标注
      dropped.push(box);
    }
    // 人工框被删只是画错了手滑：画和删都算不上"机器错在哪"，不进反馈
    render();
  }

  // 计数条与主按钮上的数字永远同源：都从这里出
  function render() {
    var auto = 0;
    var manual = 0;
    var picked = 0;
    boxes.forEach(function (box) {
      if (box.source === 'manual') { manual += 1; } else { auto += 1; }
      if (box.on) { picked += 1; }
    });
    if (countAuto) { countAuto.textContent = String(auto); }
    if (countManual) { countManual.textContent = String(manual); }
    if (countPicked) { countPicked.textContent = String(picked); }
    if (submitCount) { submitCount.textContent = String(picked); }
    if (submitBtn) { submitBtn.disabled = picked === 0 || sending; }
  }

  // ------------------------------------------------------------ 点 / 删（事件委托：自动框与人工框同一套）

  layer.addEventListener('click', function (event) {
    var target = event.target;
    if (!target || !target.closest) { return; }
    var close = target.closest('.select-box__x');
    if (close) {
      event.preventDefault();
      event.stopPropagation();
      drop(find(close.closest('.select-box')));
      return;
    }
    var el = target.closest('.select-box');
    if (el) {
      event.preventDefault();
      toggle(find(el));
    }
  });

  // ------------------------------------------------------------ 画新框

  function onDown(event) {
    if (event.button !== undefined && event.button !== 0) { return; }
    if (event.target && event.target.closest && event.target.closest('.select-box')) { return; }
    var point = toImage(event.clientX, event.clientY);
    drag = { pointerId: event.pointerId, x0: point[0], y0: point[1], ghost: null };
    if (stage.setPointerCapture) { stage.setPointerCapture(event.pointerId); }
    event.preventDefault();
  }

  function onMove(event) {
    if (!drag || event.pointerId !== drag.pointerId) { return; }
    var point = toImage(event.clientX, event.clientY);
    var rect = normalize(drag.x0, drag.y0, point[0], point[1]);
    if (!drag.ghost) {
      drag.ghost = document.createElement('div');
      drag.ghost.className = 'select-box select-box--on';
      layer.appendChild(drag.ghost);
    }
    paint(drag.ghost, { x0: rect[0], y0: rect[1], x1: rect[2], y1: rect[3] });
    event.preventDefault();
  }

  function onUp(event) {
    if (!drag || event.pointerId !== drag.pointerId) { return; }
    var point = toImage(event.clientX, event.clientY);
    var rect = normalize(drag.x0, drag.y0, point[0], point[1]);
    var ghost = drag.ghost;
    drag = null;
    if (ghost && ghost.parentNode) { ghost.parentNode.removeChild(ghost); }
    if (stage.releasePointerCapture) {
      try { stage.releasePointerCapture(event.pointerId); } catch (err) { /* 已经松了 */ }
    }
    if (Math.min(rect[2] - rect[0], rect[3] - rect[1]) < MIN_SIDE) {
      if (rect[2] || rect[3]) {
        say(tr('框太小了（任一边不到 ' + MIN_SIDE + ' 像素），拖大一点再松手。', 'The box is too small. Make each side at least ' + MIN_SIDE + ' pixels.'), 'warn');
      }
      return;
    }
    say('');
    modified = true;
    var el = make(rect);
    manualSeq += 1;
    var box = {
      el: el,
      // 人工框的 frame_key 按创建顺序编号：同一页重复提交靠它覆盖同一条
      key: 'm' + manualSeq,
      source: 'manual',
      strategy: 'manual',
      no: null,
      x0: rect[0], y0: rect[1], x1: rect[2], y1: rect[3],
      on: true
    };
    el.classList.add('select-box--on');
    boxes.push(box);
    paint(el, box);
    render();
  }

  stage.addEventListener('pointerdown', onDown);
  stage.addEventListener('pointermove', onMove);
  stage.addEventListener('pointerup', onUp);
  stage.addEventListener('pointercancel', onUp);

  // ------------------------------------------------------------ 全选 / 全不选

  if (allBtn) {
    allBtn.addEventListener('click', function () {
      modified = true;
      boxes.forEach(function (box) {
        box.on = true;
        box.el.classList.add('select-box--on');
      });
      render();
    });
  }

  if (noneBtn) {
    noneBtn.addEventListener('click', function () {
      modified = true;
      boxes.forEach(function (box) {
        box.on = false;
        box.el.classList.remove('select-box--on');
      });
      render();
    });
  }

  // ------------------------------------------------------------ 提交

  function post(url, pairs) {
    var body = new URLSearchParams();
    pairs.forEach(function (pair) { body.append(pair[0], pair[1]); });
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'text/html' },
      body: body.toString()
    }).then(function (response) {
      if (!response.ok) {
        throw new Error('提交失败（' + response.status + '），选择都还在，再点一次。');
      }
      return response;
    });
  }

  function bbox(box) { return [box.x0, box.y0, box.x1, box.y1]; }

  // 这一页的四个动作（docs/CUT-FEEDBACK-SPEC.md）：
  //   accept  勾中的自动框（human_bbox 就是它自己：人看过、原样接受）
  //   delete  没勾的自动框 + 被 × 删掉的自动框（human_bbox 为空）
  //   add     人工画出来、不对应任何自动框的
  //   redraw  v1 不做"人工框盖在哪个自动框上"的判断，所以恒为 0——
  //           要判就得先算人工框与自动框的归属关系，那是下一张卡的事。
  // 每页一次快照，中间撤销/重画都在这里合并完再发，不逐动作上报。
  function feedbackFrames() {
    var frames = [];
    boxes.forEach(function (box) {
      if (box.source === 'manual') {
        frames.push({
          frame_key: box.key,
          no: null,
          source: 'manual',
          auto_bbox: null,
          human_bbox: bbox(box),
          selected: !!box.on,
          action: 'add'
        });
        return;
      }
      frames.push({
        frame_key: box.key,
        no: box.no,
        source: box.strategy,
        auto_bbox: bbox(box),
        human_bbox: box.on ? bbox(box) : null,
        selected: !!box.on,
        action: box.on ? 'accept' : 'delete'
      });
    });
    dropped.forEach(function (box) {
      frames.push({
        frame_key: box.key,
        no: box.no,
        source: box.strategy,
        auto_bbox: bbox(box),
        human_bbox: null,
        selected: false,
        action: 'delete'
      });
    });
    return frames;
  }

  function sendFeedback() {
    var body = {
      scan_id: Number(scanId),
      image_sha256: meta.sha256,
      image_w: meta.width,
      image_h: meta.height,
      recipe: meta.recipe,
      params_hash: meta.paramsHash,
      frames: feedbackFrames()
    };
    return fetch(FEEDBACK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) {
        throw new Error(
          '题已经收进去了，但这次的修正没记下来（' + response.status +
          '）。选择都还在，再点一次只补记这一条。'
        );
      }
      return response;
    });
  }

  function submit() {
    if (sending) { return; }
    var nos = [];
    var frames = [];
    boxes.forEach(function (box) {
      if (!box.on) { return; }
      if (box.source === 'auto') {
        nos.push(['no', String(box.no)]);
      } else {
        frames.push(['frame_' + frames.length, [box.x0, box.y0, box.x1, box.y1].join(',')]);
      }
    });
    if (!committed && !nos.length && !frames.length) {
      say(tr('先勾几道题，或者自己在图上圈一道。', 'Select a question or draw a box around one.'), 'warn');
      return;
    }

    sending = true;
    render();
    say(committed ? tr('正在补记这次的修正…', 'Saving your corrections…') : tr('正在收进去…', 'Saving questions…'), 'info');
    // Workflow selections are one transaction; legacy scans keep their two routes.
    var stored;
    if (committed) {
      stored = Promise.resolve();
    } else if (workflowMode) {
      var subjectInput = document.getElementById('workflow-subject');
      var subject = subjectInput.value.trim();
      var questionType = document.getElementById('workflow-question-type').value.trim();
      if (!subject || !questionType) {
        sending = false;
        render();
        say(tr('请填写科目和题型。', 'Enter a subject and question type.'), 'warn');
        return;
      }
      stored = fetch('/api/scans/' + scanId + '/workflow-selection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          auto_numbers: nos.map(function (pair) { return Number(pair[1]); }),
          manual_frames: frames.map(function (pair) { return pair[1].split(',').map(Number); }),
          subject: english && subject === subjectInput.dataset.display ? subjectInput.dataset.canonical :
            (english && subject === 'Math' ? '数学' : subject),
          question_type: english && questionType === 'Fill in' ? '填空' : questionType
        })
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            var details = {
              '有题目文字不完整，请取消选择或补充题干': 'Some recognized text is incomplete. Deselect that question or add it manually.',
              '手动画框太小或超出照片': 'A manual box is too small or outside the image.',
              '流程已在其他设备更新，请刷新后重试': 'This workflow was updated on another device. Refresh and try again.',
              '科目与目标题集不一致，请修改科目': 'The subject must match the destination collection.',
              '目标题集不可用': 'The destination collection is unavailable.'
            };
            throw new Error(english ? (details[data.detail] || 'Could not save the selection. Refresh and try again.') :
              (data.detail || '暂时无法完成圈选，请再试一次。'));
          }
          workflowNextUrl = data.next_url;
        });
      });
    } else {
      stored = Promise.all([post(questionsUrl(), nos), post(frameUrl(), frames)]);
    }
    stored
      .then(function () {
        committed = true;
        if (!workflowMode) { return sendFeedback(); }
        // Feedback is optional: the workflow has already saved the questions.
        return sendFeedback().catch(function () { return null; });
      })
      .then(function () {
        window.location.href = workflowMode ? withLanguage(workflowNextUrl) : '/scans/' + scanId;
      })
      .catch(function (error) {
        sending = false;
        render();
        say(error && error.message ? error.message : tr('没成功，再点一次。', 'Something went wrong. Try again.'), 'danger');
      });
  }

  if (submitBtn) { submitBtn.addEventListener('click', submit); }

  // ------------------------------------------------------------ 起跑

  var nodes = layer ? layer.querySelectorAll('.select-box') : [];
  for (var i = 0; i < nodes.length; i += 1) { adopt(nodes[i]); }
  render();

  function draw() {
    boxes.forEach(function (box) { paint(box.el, box); });
  }

  if (img.complete && img.naturalWidth) { draw(); }
  else { img.addEventListener('load', draw); }
  window.addEventListener('resize', draw);
})();
