/**
 * 화면 장치 두 개 — 좁은 화면의 메뉴, 공시 상세의 절 탭.
 *
 * 프레임워크를 쓰지 않는다(PLAN.md 9.5). 재배포가 1GB 서버에서
 * `docker compose up -d --build` 로 돌아 npm 빌드를 얹으면 배포마다 OOM 위험이고,
 * 이 파일이 하는 일은 통째로 50줄이다.
 *
 * **둘 다 없어도 화면이 동작해야 한다.** 메뉴 항목은 템플릿에 이미 적혀 있고
 * (base.html 의 .nav-sheet), 절 탭은 평범한 #앵커 링크다. 여기서 하는 것은
 * 여는 일과 밑줄을 옮기는 일뿐이다.
 */
(function () {
  'use strict';

  /* 좁은 화면 메뉴 ------------------------------------------------------ */
  var toggle = document.querySelector('.nav-toggle');
  var sheet = document.getElementById('nav-sheet');

  if (toggle && sheet) {
    toggle.addEventListener('click', function () {
      var open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!open));
      sheet.dataset.open = String(!open);
      // 메뉴가 화면을 덮는 동안 뒤의 본문이 따라 스크롤되면 어디를 보고 있는지 잃는다.
      document.body.style.overflow = open ? '' : 'hidden';
    });

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || toggle.getAttribute('aria-expanded') !== 'true') {
        return;
      }
      toggle.setAttribute('aria-expanded', 'false');
      sheet.dataset.open = 'false';
      document.body.style.overflow = '';
      toggle.focus();
    });
  }

  /* 공시 상세의 절 탭 --------------------------------------------------- */
  var links = Array.prototype.slice.call(
    document.querySelectorAll('.section-tabs a'));

  if (links.length && 'IntersectionObserver' in window) {
    // 아래쪽 60%를 잘라내는 이유: 화면에 절이 둘 이상 들어와 있을 때 밑줄이 두 곳에서
    // 깜박이지 않게, 위쪽 띠만 판정 영역으로 삼는다. 위 140px은 머리말 64px + 탭 높이.
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) {
          return;
        }
        links.forEach(function (link) {
          link.setAttribute(
            'aria-current',
            String(link.getAttribute('href') === '#' + entry.target.id));
        });
      });
    }, { rootMargin: '-140px 0px -60% 0px' });

    links.forEach(function (link) {
      var section = document.querySelector(link.getAttribute('href'));
      if (section) {
        observer.observe(section);
      }
    });
  }
})();
