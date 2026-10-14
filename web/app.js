const $ = (id) => document.getElementById(id);
const form = $('url-form'), input = $('video-url'), message = $('message');
let current = null, pollTimer = null, activeRun = 0, cloudAbort = null;
const cloudMode = fetch('/api/mode').then(response => response.ok).catch(() => false);
cloudMode.then(isCloud => {
  if (isCloud) {
    document.querySelector('.top-status').innerHTML = '<span class="live-dot"></span> 온라인에서 변환';
    document.querySelector('.hint').textContent += ' · 온라인 30분 이하';
  }
});

function showError(text) {
  message.textContent = text;
  message.classList.remove('hidden');
}
function clearError() { message.classList.add('hidden'); message.textContent = ''; }
function fmtTime(sec) { return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`; }
function stopPolling() {
  clearTimeout(pollTimer); pollTimer = null;
  if (cloudAbort) { cloudAbort.abort(); cloudAbort = null; }
  activeRun += 1;
}
function startDownload(id) {
  const link = document.createElement('a');
  link.href = `/api/jobs/${id}/download`;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
}
async function request(path, data) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || body.detail || '요청에 실패했습니다.');
  return body;
}

async function convertInCloud(run) {
  const controller = new AbortController();
  cloudAbort = controller;
  $('progress-label').textContent = '오디오 추출 및 MP3 인코딩 중';
  $('progress-num').textContent = '진행 중';
  $('progress-fill').classList.add('indeterminate');
  try {
    const response = await fetch('/api/convert', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: current.url}), signal: controller.signal
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || 'MP3 변환에 실패했습니다.');
    }
    if (!response.body) throw new Error('다운로드 스트림을 열지 못했습니다.');
    $('progress-fill').classList.remove('indeterminate');
    $('progress-label').textContent = 'MP3 다운로드 준비 중';
    const total = Number(response.headers.get('Content-Length')) || 0;
    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (total) {
        const percent = Math.min(99, Math.round(received / total * 100));
        $('progress-num').textContent = `${percent}%`;
        $('progress-fill').style.width = `${percent}%`;
      } else {
        $('progress-num').textContent = `${(received / 1024 / 1024).toFixed(1)} MB`;
      }
    }
    if (run !== activeRun || !received) throw new Error('다운로드가 중단되었습니다.');
    const blobUrl = URL.createObjectURL(new Blob(chunks, {type: 'audio/mpeg'}));
    const link = document.createElement('a');
    link.href = blobUrl;
    link.download = `${current.title.replace(/[<>:"/\\|?*\x00-\x1f]/g, '').trim().slice(0, 120) || current.id}.mp3`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    $('progress-panel').classList.add('hidden');
    $('result-size').textContent = `${(received / 1024 / 1024).toFixed(1)} MB · 320 kbps MP3 · 브라우저 다운로드 목록에서 확인하세요.`;
    $('result').classList.remove('hidden');
  } finally {
    if (cloudAbort === controller) cloudAbort = null;
    $('progress-fill').classList.remove('indeterminate');
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  stopPolling();
  current = null;
  clearError();
  $('preview').classList.add('hidden');
  $('result').classList.add('hidden');
  $('progress-panel').classList.add('hidden');
  $('convert-btn').classList.remove('hidden');
  $('convert-btn').disabled = false;
  const button = $('inspect-btn');
  button.disabled = true;
  button.textContent = '확인 중…';
  try {
    current = await request('/api/inspect', {url: input.value.trim()});
    $('thumb').src = current.thumbnail;
    $('video-title').textContent = current.title;
    $('video-meta').textContent = `${current.channel} · ${fmtTime(current.duration)}`;
    $('source-quality').textContent = `원본 오디오 ${current.source_codec}${current.source_kbps ? ` · 약 ${current.source_kbps} kbps` : ''}`;
    $('preview').classList.remove('hidden');
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; button.innerHTML = '영상 확인 <span aria-hidden="true">↗</span>'; }
});

$('paste-btn').addEventListener('click', async () => {
  try { input.value = await navigator.clipboard.readText(); input.focus(); }
  catch { showError('클립보드를 읽을 수 없습니다. 링크를 직접 붙여넣어 주세요.'); }
});

$('convert-btn').addEventListener('click', async () => {
  if (!current) return;
  stopPolling();
  const run = activeRun;
  clearError();
  $('result').classList.add('hidden');
  $('convert-btn').disabled = true;
  $('convert-btn').classList.add('hidden');
  $('progress-panel').classList.remove('hidden');
  $('progress-label').textContent = '오디오 다운로드 중';
  $('progress-num').textContent = '0%';
  $('progress-fill').style.width = '0%';
  try {
    if (await cloudMode) {
      await convertInCloud(run);
      return;
    }
    const {id} = await request('/api/jobs', {url: current.url});
    const check = async () => {
      if (run !== activeRun) return;
      try {
        const response = await fetch(`/api/jobs/${id}`, {cache:'no-store'});
        const job = await response.json();
        if (run !== activeRun) return;
        if (!response.ok) throw new Error(job.error);
        $('progress-label').textContent = job.status === 'encoding' ? 'MP3 인코딩 중' : '오디오 다운로드 중';
        $('progress-num').textContent = `${job.progress}%`;
        $('progress-fill').style.width = `${job.progress}%`;
        if (job.status === 'ready') {
          $('progress-label').textContent = '브라우저 다운로드 시작 중';
          startDownload(id);
          $('progress-panel').classList.add('hidden');
          $('result-size').textContent = `${(job.size / 1024 / 1024).toFixed(1)} MB · 320 kbps MP3 · 브라우저 다운로드 목록에서 확인하세요.`;
          $('result').classList.remove('hidden');
        } else if (job.status === 'error') {
          $('progress-panel').classList.add('hidden');
          $('convert-btn').disabled = false;
          $('convert-btn').classList.remove('hidden');
          showError(job.error || '변환에 실패했습니다.');
        } else {
          pollTimer = setTimeout(check, 1200);
        }
      } catch (error) {
        if (run !== activeRun) return;
        $('progress-panel').classList.add('hidden');
        $('convert-btn').disabled = false;
        $('convert-btn').classList.remove('hidden');
        showError(error.message);
      }
    };
    await check();
  } catch (error) {
    if (run !== activeRun) return;
    $('progress-panel').classList.add('hidden');
    $('convert-btn').disabled = false;
    $('convert-btn').classList.remove('hidden');
    showError(error.message);
  }
});
