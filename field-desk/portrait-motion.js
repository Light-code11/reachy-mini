// The local daemon reports metres and radians in the robot frame (+X forward,
// +Y robot-left, +Z up). The viewer faces the robot.
function decodeReachyPose(state) {
  const head = state?.head_pose;
  const antennas = state?.antennas_position;
  if (!head || !Array.isArray(antennas) || antennas.length !== 2) return null;
  const values = [head.yaw, head.pitch, head.roll, head.x, head.y, head.z,
    state.body_yaw, ...antennas];
  if (!values.every(Number.isFinite)) return null;
  const degrees = 180 / Math.PI;
  return {
    turn: head.yaw * degrees, lift: -head.pitch * degrees, tilt: -head.roll * degrees,
    body: state.body_yaw * degrees, left: -antennas[1] * degrees, right: -antennas[0] * degrees,
    forward: head.x, lateral: head.y, height: head.z
  };
}

function animateReachyPortrait(portrait) {
  const part = id => portrait.querySelector('#' + id);
  const head = part('headPose');
  const shell = part('headShell');
  const face = part('faceplate');
  const mounts = part('antennaMounts');
  const leftAntenna = part('leftAntenna');
  const rightAntenna = part('rightAntenna');
  const highlight = part('headPorcelain');
  const button = part('portraitMotion');
  const torso = part('torsoPose');
  const shoulders = part('shoulderDetails');
  const source = part('viewSource');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  const neutral = { turn: 0, lift: 0, tilt: 0, left: 0, right: 0 };
  const listen = { turn: 17, lift: 3, tilt: 3, left: -3, right: 7 };
  const reply = { turn: 26, lift: 9, tilt: -2, left: 5, right: -4 };
  const glance = { turn: -19, lift: -4, tilt: -5, left: -8, right: 3 };
  const sequence = [
    [0, neutral], [0.7, neutral],
    [1.5, listen], [2.2, { ...listen, left: -9, right: 13 }],
    [2.7, listen], [4.4, listen],
    [4.95, reply], [5.28, { ...reply, lift: 2, right: 9 }],
    [5.68, { ...reply, lift: 6, left: -4 }], [6.65, reply],
    [7.5, neutral], [9.1, neutral],
    [10.05, glance], [11.3, glance],
    [11.8, { ...glance, left: 2, right: -8 }],
    [12.65, neutral], [14, neutral]
  ];

  function samplePose(time) {
    const next = sequence.findIndex(([at]) => at > time);
    const [start, from] = sequence[next - 1];
    const [end, to] = sequence[next];
    const t = (time - start) / (end - start);
    const ease = t * t * (3 - 2 * t);
    const pose = {};
    for (const axis of Object.keys(neutral)) {
      pose[axis] = from[axis] + (to[axis] - from[axis]) * ease;
    }
    return pose;
  }

  // Project the front lenses, crown and shell at their respective depths.
  // All three share a neck pivot, so the lenses stay attached during turns.
  function draw({ turn, lift, tilt, left, right, body = 0, forward = 0, lateral = 0, height = 0 }) {
    const radians = Math.PI / 180;
    const yaw = turn * radians;
    const pitch = lift * radians;
    const horizontal = Math.sin(yaw);
    const vertical = Math.sin(pitch);
    function layer(element, depth, width, height, centerY) {
      element.setAttribute('transform',
        'translate(' + (450 + depth * horizontal) + ' ' + (centerY - depth * vertical) +
        ') scale(' + width + ' ' + height + ') translate(-450 ' + (-centerY) + ')');
    }
    const sideways = lateral * 4600 + 27 * horizontal;
    const rise = height * 4600 + 65 * vertical;
    const depthScale = 1 + Math.max(-.18, Math.min(.18, forward * 3));
    head.setAttribute('transform',
      `translate(${sideways} ${-rise}) rotate(${tilt} 450 823) translate(450 823) scale(${depthScale}) translate(-450 -823)`);
    const bodyAngle = body * radians;
    layer(torso, 0, .9 + .1 * Math.abs(Math.cos(bodyAngle)), 1, 1510);
    layer(shoulders, 72, Math.cos(bodyAngle), 1, 925);
    shoulders.setAttribute('opacity', Math.max(0, Math.min(1, Math.cos(bodyAngle) * 3)));
    face.setAttribute('opacity', Math.max(0, Math.min(1, Math.cos(yaw) * 3)));
    layer(shell, 0, .73 + .27 * Math.abs(Math.cos(yaw)), .8 + .2 * Math.abs(Math.cos(pitch)), 650);
    layer(face, 172, Math.cos(yaw), Math.cos(pitch), 650);
    layer(mounts, 25, Math.cos(yaw), Math.cos(pitch), 462);
    leftAntenna.setAttribute('transform', 'rotate(' + left + ' 0 453)');
    rightAntenna.setAttribute('transform', 'rotate(' + right + ' 0 453)');
    highlight.setAttribute('cx', (46 - 19 * horizontal) + '%');
  }

  let elapsed = 0;
  let lastFrame = null;
  let frame = 0;
  let nodStart = null;
  let paused = reducedMotion.matches;
  let livePose = null;
  let displayedLivePose = null;
  const duration = sequence[sequence.length - 1][0];
  const nodDuration = .85;

  function tick(now) {
    const delta = lastFrame === null ? 0 : Math.min((now - lastFrame) / 1000, .1);
    elapsed += delta;
    lastFrame = now;
    if (livePose) {
      // Smooth the interval between daemon samples without adding demo gestures.
      const blend = 1 - Math.exp(-delta * 34);
      for (const key of Object.keys(livePose)) {
        displayedLivePose[key] += (livePose[key] - displayedLivePose[key]) * blend;
      }
      draw(displayedLivePose);
      frame = requestAnimationFrame(tick);
      return;
    }
    const pose = samplePose(elapsed % duration);
    const follow = samplePose(Math.max(0, elapsed - .3) % duration);
    pose.body = follow.turn * .57;
    pose.lateral = follow.turn * .00036;
    pose.height = pose.lift * .00075;
    if (nodStart !== null) {
      const phase = Math.min(1, (elapsed - nodStart) / nodDuration);
      const envelope = Math.sin(Math.PI * phase) ** 2;
      pose.lift -= 12 * envelope;
      pose.left -= 5 * envelope;
      pose.right += 7 * envelope;
      if (phase === 1) nodStart = null;
    }
    draw(pose);
    frame = requestAnimationFrame(tick);
  }

  function updateCaption() {
    const mode = livePose ? 'Live pose' : 'Demo motion';
    const caption = paused ? mode + ' · paused' : mode;
    if (source.textContent !== caption) source.textContent = caption;
  }

  function updatePlayback() {
    cancelAnimationFrame(frame);
    lastFrame = null;
    const action = paused ? 'Resume Reachy animation' : 'Pause Reachy animation';
    button.setAttribute('aria-label', action);
    button.title = action;
    updateCaption();
    if (!paused && !document.hidden) {
      frame = requestAnimationFrame(tick);
    }
  }

  button.addEventListener('click', () => {
    paused = !paused;
    updatePlayback();
  });
  document.addEventListener('visibilitychange', updatePlayback);
  reducedMotion.addEventListener('change', () => {
    paused = reducedMotion.matches;
    updatePlayback();
  });
  updatePlayback();

  return {
    acknowledgeSelection() {
      if (!paused && !livePose && nodStart === null) nodStart = elapsed;
    },
    mirror(pose) {
      if (!livePose) {
        displayedLivePose = { ...pose };
        nodStart = null;
        portrait.toggleAttribute('data-live', true);
      }
      livePose = pose;
      updateCaption();
    },
    preview() {
      livePose = null;
      displayedLivePose = null;
      nodStart = null;
      elapsed = 0;
      draw(neutral);
      portrait.toggleAttribute('data-live', false);
      updateCaption();
    }
  };
}

// Subscribe only when requested. A valid sample, rather than an open socket,
// marks the viewer as live. Cancel or expire a connection without stale events.
function createPoseConnection(viewer, onStatus) {
  let socket = null;
  let timeout;
  let connected = false;

  function close(message) {
    clearTimeout(timeout);
    const old = socket;
    socket = null;
    if (old) old.close();
    connected = false;
    viewer.preview();
    onStatus('offline', message);
  }

  function toggle() {
    if (socket) {
      close('Showing demo motion.');
      return;
    }
    onStatus('connecting', 'Looking for the Reachy Mini service on this PC…');
    const unavailable = 'No pose stream. Start the Reachy Mini daemon on this PC and try again.';
    try {
      socket = new WebSocket('ws://localhost:8000/api/state/ws/full?frequency=20&use_pose_matrix=false');
    } catch {
      close(unavailable);
      return;
    }
    const current = socket;
    timeout = setTimeout(() => close(unavailable), 4500);
    current.onmessage = event => {
      if (socket !== current) return;
      let pose;
      try { pose = decodeReachyPose(JSON.parse(event.data)); }
      catch { return; }
      if (!pose) return;
      clearTimeout(timeout);
      timeout = setTimeout(() => close('Pose updates stopped. Showing demo motion.'), 3000);
      viewer.mirror(pose);
      if (!connected) {
        connected = true;
        onStatus('live', 'Mirroring the local Reachy pose stream.');
      }
    };
    current.onerror = current.onclose = () => {
      if (socket === current) close(connected ? 'Pose stream disconnected. Showing demo motion.' : unavailable);
    };
  }
  return { toggle, get connected() { return connected; } };
}
