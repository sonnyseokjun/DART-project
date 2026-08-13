/* 검수 화면 원문 대조 패널의 유일한 스크립트.
 *
 * 하는 일은 하나다: '먼저 확인할 수치' 칩을 누르면 원문 스크롤 영역에서 그 수치가
 * 나오는 위치로 이동하고, 다시 누르면 다음 위치로 넘어간다. 원문이 수만 자라
 * 하이라이트만 해 두면 검수자가 스크롤로 찾아야 해서 실제로는 못 찾는다.
 *
 * 외부 라이브러리를 쓰지 않는다(프로젝트 방침). data-term 비교는 속성 선택자 대신
 * dataset 값 비교로 한다 — 수치 표기에 따옴표·쉼표가 섞여도 안전하다. */
(function () {
  'use strict';

  var panel = document.getElementById('review-panel');
  var raw = document.getElementById('rp-raw');
  if (!panel || !raw) { return; }

  var marks = Array.prototype.slice.call(raw.querySelectorAll('mark.rp-hit'));
  var cursors = {};   // 수치 표기 → 다음에 보여줄 적중 순번
  var active = null;

  function focusHit(term) {
    var hits = marks.filter(function (mark) { return mark.dataset.term === term; });
    if (!hits.length) { return; }

    var index = (cursors[term] || 0) % hits.length;
    cursors[term] = index + 1;

    var target = hits[index];
    if (active) { active.classList.remove('rp-hit-active'); }
    target.classList.add('rp-hit-active');
    active = target;
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  panel.addEventListener('click', function (event) {
    var chip = event.target.closest('.rp-chip-found');
    if (chip && chip.dataset.term) { focusHit(chip.dataset.term); }
  });
})();
