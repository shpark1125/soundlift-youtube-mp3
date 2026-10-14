const $ = (id) => document.getElementById(id);
const form = $('url-form'), input = $('video-url'), message = $('message');
let current = null, pollTimer = null;

function showError(text) {
  message.textContent = text;
  message.classList.remove('hidden');
}
function clearError() { message.classList.add('hidden'); message.textContent = ''; }
function fmtTime(sec) { return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`; }
async function request(path, data) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '요청에 실패했습니다.');
  return body;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearError();
  $('preview').classList.add('hidden');
  $('result').classList.add('hidden');
  $('progress-panel').classList.add('hidden');
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
  clearError();
  $('result').classList.add('hidden');
  $('convert-btn').disabled = true;
  try {
    const {id} = await request('/api/jobs', {url: current.url});
    $('progress-panel').classList.remove('hidden');
    const check = async () => {
      try {
        const response = await fetch(`/api/jobs/${id}`, {cache:'no-store'});
        const job = await response.json();
        if (!response.ok) throw new Error(job.error);
        $('progress-label').textContent = job.status === 'encoding' ? 'MP3 인코딩 중' : '오디오 다운로드 중';
        $('progress-num').textContent = `${job.progress}%`;
        $('progress-fill').style.width = `${job.progress}%`;
        if (job.status === 'ready') {
          clearInterval(pollTimer);
          $('progress-panel').classList.add('hidden');
          $('result-size').textContent = `${(job.size / 1024 / 1024).toFixed(1)} MB · 320 kbps MP3`;
          $('download-link').href = `/api/jobs/${id}/download`;
          $('result').classList.remove('hidden');
          $('convert-btn').disabled = false;
        } else if (job.status === 'error') {
          clearInterval(pollTimer);
          $('progress-panel').classList.add('hidden');
          $('convert-btn').disabled = false;
          showError(job.error || '변환에 실패했습니다.');
        }
      } catch (error) {
        clearInterval(pollTimer);
        $('progress-panel').classList.add('hidden');
        $('convert-btn').disabled = false;
        showError(error.message);
      }
    };
    await check();
    if ($('convert-btn').disabled) pollTimer = setInterval(check, 1200);
  } catch (error) { $('convert-btn').disabled = false; showError(error.message); }
});
