/**
 * 목록 화면 자동 갱신 (PLAN.md 9.3).
 *
 * 서버가 밀어주는 방식(WebSocket/SSE)을 쓰지 않는다. 그쪽은 접속자 한 명당 서버
 * 연결을 계속 점유하는데 현재 gunicorn 워커가 2개라, **두 명이 페이지를 열어두기만
 * 해도 사이트가 멈춘다.** 게다가 공시를 감지하는 데 이미 1~3분이 걸리므로 전달을
 * 0초로 만들어도 전체는 빨라지지 않는다 — 병목이 아닌 곳의 최적화다.
 *
 * 대신 브라우저가 주기적으로 "새 게 있나" 만 물어본다. 응답은 작은 JSON이고 DART가
 * 아니라 우리 DB만 읽으므로, 방문자가 늘어도 DART 호출은 0이다(PLAN.md 12.1).
 *
 * 흐름:
 *   1. 서명(signature)을 받아 기준으로 삼는다
 *   2. 주기마다 다시 물어본다 — 서명이 같으면 아무것도 하지 않는다
 *   3. 달라지면 지금 보고 있는 URL을 partial=1로 다시 받아 목록만 교체한다
 *
 * 3번에서 화면을 통째로 새로고침하지 않는 이유: 읽던 위치와 스크롤이 날아간다.
 * 목록 조각은 서버가 같은 뷰·같은 템플릿으로 그리므로 필터·페이지네이션 로직이
 * 두 벌이 되지 않는다.
 */
(function () {
  'use strict';

  var endpoint = document.body.getAttribute('data-live-endpoint');
  var feed = document.querySelector('[data-live-feed]');
  if (!endpoint || !feed || !window.fetch) {
    return;
  }

  var signature = null;
  var intervalMs = 0;
  var timer = null;
  var stopped = false;

  function schedule() {
    if (stopped || !intervalMs) {
      return;
    }
    window.clearTimeout(timer);
    timer = window.setTimeout(tick, intervalMs);
  }

  function tick() {
    // 탭을 안 보고 있으면 묻지 않는다. 열어만 두고 딴 일을 하는 경우가 대부분이라
    // 여기서 걸러야 실제 요청이 크게 준다. 돌아오면 visibilitychange가 깨운다.
    if (document.visibilityState === 'hidden') {
      schedule();
      return;
    }
    check();
  }

  function check() {
    fetch(endpoint, { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('status ' + response.status);
        }
        return response.json();
      })
      .then(function (data) {
        // 서버가 매번 주기를 함께 알려준다. 평일 낮과 야간의 파이프라인 주기가
        // 다르므로(deploy/crontab) 판단을 서버에 두어 한 곳에서만 바꾸게 한다.
        intervalMs = (data.interval_seconds || 0) * 1000;

        if (signature === null) {
          signature = data.signature;
        } else if (data.signature !== signature) {
          signature = data.signature;
          refreshFeed();
        }
        schedule();
      })
      .catch(function () {
        // 일시적인 실패로 갱신을 영영 멈추면 안 된다. 다음 주기에 다시 시도한다.
        // 사용자에게 알리지 않는다 — 배경에서 도는 편의 기능이라 오류를 띄우면
        // 얻는 것보다 잃는 것이 크다.
        schedule();
      });
  }

  function refreshFeed() {
    var url = new URL(window.location.href);
    url.searchParams.set('partial', '1');

    fetch(url.toString(), { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('status ' + response.status);
        }
        return response.text();
      })
      .then(function (html) {
        var holder = document.createElement('div');
        holder.innerHTML = html;
        var next = holder.querySelector('[data-live-feed]');
        if (!next) {
          return;
        }
        feed.innerHTML = next.innerHTML;
        announce();
      })
      .catch(function () {
        /* 조각을 못 받으면 이번 회차는 건너뛴다. 서명은 이미 갱신됐으므로
           다음 변경 때 다시 시도된다. */
      });
  }

  function announce() {
    // 내용만 소리 없이 바뀌면 사용자는 알아채지 못한다. 짧게 알리고 사라진다.
    var note = feed.querySelector('[data-live-note]');
    if (!note) {
      return;
    }
    note.hidden = false;
    window.setTimeout(function () {
      note.hidden = true;
    }, 6000);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && !stopped) {
      check();
    }
  });

  check();
})();
