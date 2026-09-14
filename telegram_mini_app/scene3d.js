/**
 * NQX Nightwatch — background scene.
 *
 * The scene is one machine: a cast-iron assay balance standing on a granite
 * surface plate, lit by a single luminaire. The luminaire is a hooded
 * projector head held by a three-arm optical spider, with a mechanical leaf
 * iris in front of its lens. It is the only light in the room, so every
 * highlight, every shadow and the colour of the whole scene come out of it.
 *
 * Only one thing in here has a timeline of its own: the beam. It is a damped
 * second-order body with real mass, mechanical end stops and an arrest fork
 * that clamps it when no scenario is armed. Everything else — the stirrups,
 * the pans, the pointer, its trail, the luminaire's gaze and its iris — is a
 * follower driven by the beam's state.
 *
 * The rig is placed from measured DOM geometry so it can never overlap the
 * command column. See computeLayout().
 */

import * as THREE from "three";
import gsap from "gsap";
import {
  BloomEffect,
  EffectComposer,
  EffectPass,
  RenderPass,
  VignetteEffect
} from "postprocessing";

const DEG = Math.PI / 180;

const PALETTE = {
  acid: 0xff3b4f,
  rust: 0xce5a43,
  violet: 0xafa4d9,
  bone: 0xbdb7a8,
  black: 0x070707
};

// Beam mechanics. zeta 0.25 at 7.0 rad/s gives a 0.90 s damped period, ~44%
// overshoot and a ~2.3 s settle — the ring of a real analytical balance,
// shortened to something a UI can live with.
const BEAM = {
  omega: 7.0,
  zeta: 0.25,
  tilt: 5.0 * DEG,
  stop: 8.2 * DEG,
  restitution: 0.26
};

// Hidden reveal. Type the sequence anywhere, or press and hold the wordmark.
// The luminaire stops watching the pans, turns on the viewer and opens all the
// way: bezel, three-arm spider and blazing aperture become the sign outright.
const SIGIL = {
  sequence: "1776",
  holdMs: 1600,
  durationMs: 7000,
  color: 0xe8d9a8,
  caption: "ANNVIT CŒPTIS · NOVVS ORDO SECLORVM · MDCCLXXVI"
};

const MODES = {
  // A positive tilt drops the left pan. The luminaire hangs above the beam, so
  // the pan that rises comes up into its light and the pan that sinks falls
  // away into shadow — LONG drops the right pan and lights the left.
  neutral: { tilt: 0, color: PALETTE.violet, aperture: 0.42, intensity: 250 },
  long: { tilt: -BEAM.tilt, color: PALETTE.acid, aperture: 0.95, intensity: 300 },
  short: { tilt: BEAM.tilt, color: PALETTE.rust, aperture: 0.95, intensity: 380 },
  // 「発注可」= 武装状態。同じ傾きのまま灯りだけが強くなる。
  // 絞りと強度はシーンの校正範囲内に収める(超えるとブルームで画面が白飛びする。
  // 2026-08-15 に実機で発生: aperture 1.28 / intensity 640 は全面白になった)。
  "long-armed": { tilt: -BEAM.tilt, color: PALETTE.acid, aperture: 1.0, intensity: 420 },
  "short-armed": { tilt: BEAM.tilt, color: PALETTE.rust, aperture: 1.0, intensity: 470 }
};

const BEAM_HALF = 1.62;
const PIVOT_Y = 2.42;
const POINTER_LEN = 1.35;
const SCALE_RADIUS = 1.45;
// Leaf swing from shut to wide open. The blades are wide, so the clear
// aperture is set by the perpendicular distance from the axis to a blade edge
// (Rp·sin α − halfWidth), not by the blade tips.
const IRIS_SWING = 1.35;
// Housing spill inside the head, lighting the bezel and the leaf edges.
const GLOW_BASE = 0.5;
const TRAIL_SAMPLES = 26;
const GHOSTS = 3;

function supportsWebGL() {
  try {
    const probe = document.createElement("canvas");
    return Boolean(window.WebGLRenderingContext && (probe.getContext("webgl2") || probe.getContext("webgl")));
  } catch {
    return false;
  }
}

function createRegistry() {
  const geometries = new Set();
  const materials = new Set();
  return {
    geo(geometry) { geometries.add(geometry); return geometry; },
    mat(material) { materials.add(material); return material; },
    dispose() {
      geometries.forEach((geometry) => geometry.dispose());
      materials.forEach((material) => material.dispose());
      geometries.clear();
      materials.clear();
    }
  };
}

/**
 * A damped second-order body with hard mechanical stops. This is what gives
 * the beam its weight: it cannot be told where to be, only what to seek.
 */
class Body {
  constructor({ omega, zeta, min = -Infinity, max = Infinity, restitution = 0 }) {
    this.omega = omega;
    this.zeta = zeta;
    this.min = min;
    this.max = max;
    this.restitution = restitution;
    this.value = 0;
    this.velocity = 0;
    this.accel = 0;
    this.target = 0;
    this.locked = false;
  }

  step(dt) {
    if (this.locked) {
      this.velocity = 0;
      this.accel = 0;
      return;
    }
    const steps = Math.max(1, Math.ceil(dt / (1 / 120)));
    const h = dt / steps;
    for (let i = 0; i < steps; i += 1) {
      this.accel = -this.omega * this.omega * (this.value - this.target)
        - 2 * this.zeta * this.omega * this.velocity;
      this.velocity += this.accel * h;
      this.value += this.velocity * h;
      if (this.value > this.max) {
        this.value = this.max;
        if (this.velocity > 0) this.velocity = -this.velocity * this.restitution;
      } else if (this.value < this.min) {
        this.value = this.min;
        if (this.velocity < 0) this.velocity = -this.velocity * this.restitution;
      }
    }
  }

  settled(valueEps, speedEps) {
    return Math.abs(this.value - this.target) < valueEps && Math.abs(this.velocity) < speedEps;
  }

  clampTo(value) {
    this.value = value;
    this.target = value;
    this.velocity = 0;
    this.accel = 0;
  }
}

function buildMaterials(reg) {
  return {
    granite: reg.mat(new THREE.MeshStandardMaterial({ color: 0x1c1e1a, roughness: 0.9, metalness: 0.06 })),
    floor: reg.mat(new THREE.MeshStandardMaterial({ color: 0x101210, roughness: 0.96, metalness: 0 })),
    iron: reg.mat(new THREE.MeshStandardMaterial({ color: 0x3c4039, roughness: 0.58, metalness: 0.42 })),
    steel: reg.mat(new THREE.MeshStandardMaterial({ color: 0x6a6f66, roughness: 0.3, metalness: 0.82 })),
    polished: reg.mat(new THREE.MeshStandardMaterial({ color: 0x9aa094, roughness: 0.14, metalness: 0.9 })),
    brass: reg.mat(new THREE.MeshStandardMaterial({ color: 0xa98d52, roughness: 0.3, metalness: 0.86 })),
    brassDark: reg.mat(new THREE.MeshStandardMaterial({ color: 0x6a5a36, roughness: 0.45, metalness: 0.8 })),
    agate: reg.mat(new THREE.MeshStandardMaterial({ color: 0x3a3c37, roughness: 0.14, metalness: 0.12 })),
    reflector: reg.mat(new THREE.MeshStandardMaterial({ color: 0xd8bd78, roughness: 0.06, metalness: 1, side: THREE.BackSide }))
  };
}

/**
 * Metals with no environment to reflect render black. This is the room the
 * balance stands in, baked to an irradiance map: dark walls, one warm panel
 * where the luminaire hangs, one cold panel low and opposite for bounce.
 * Kept inline so the page has no dependency beyond the three core bundle.
 */
function buildEnvironment(renderer) {
  const room = new THREE.Scene();
  const parts = [];
  const panel = (color, size, position, rotation) => {
    const geometry = new THREE.PlaneGeometry(size[0], size[1]);
    const material = new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(position[0], position[1], position[2]);
    mesh.rotation.set(rotation[0], rotation[1], rotation[2]);
    room.add(mesh);
    parts.push(geometry, material);
    return mesh;
  };

  const shellGeometry = new THREE.BoxGeometry(14, 9, 14);
  const shellMaterial = new THREE.MeshBasicMaterial({ color: 0x0b0c0a, side: THREE.BackSide });
  room.add(new THREE.Mesh(shellGeometry, shellMaterial));
  parts.push(shellGeometry, shellMaterial);

  panel(0xb8a173, [5, 3.4], [1.6, 3.6, -3.2], [0.72, 0, 0]);
  panel(0x525b68, [7, 2.2], [-3.4, -2.2, 1.6], [-0.5, 0.9, 0]);
  panel(0x30353b, [6, 4], [0, 0.4, 6], [0, Math.PI, 0]);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const target = pmrem.fromScene(room, 0.02);
  pmrem.dispose();
  parts.forEach((part) => part.dispose());
  return target.texture;
}

function addMesh(parent, geometry, material, position, rotation) {
  const mesh = new THREE.Mesh(geometry, material);
  if (position) mesh.position.set(position[0], position[1], position[2]);
  if (rotation) mesh.rotation.set(rotation[0], rotation[1], rotation[2]);
  parent.add(mesh);
  return mesh;
}

/** Granite surface plate on three levelling feet. */
function buildPlinth(reg, mats) {
  const group = new THREE.Group();
  const foot = reg.geo(new THREE.CylinderGeometry(0.12, 0.15, 0.26, 14));
  [[-1.32, -0.4], [1.32, -0.4], [0, 0.46]].forEach(([x, z]) => {
    const mesh = addMesh(group, foot, mats.iron, [x, 0.13, z]);
    mesh.castShadow = true;
  });
  const slab = addMesh(group, reg.geo(new THREE.BoxGeometry(3.4, 0.2, 1.34)), mats.granite, [0, 0.36, 0]);
  slab.receiveShadow = true;
  slab.castShadow = true;
  const cap = addMesh(group, reg.geo(new THREE.BoxGeometry(3.5, 0.035, 1.42)), mats.granite, [0, 0.463, 0]);
  cap.receiveShadow = true;
  return group;
}

/** Cast column carrying the agate knife-edge seat. */
function buildColumn(reg, mats) {
  const group = new THREE.Group();
  addMesh(group, reg.geo(new THREE.CylinderGeometry(0.42, 0.48, 0.14, 22)), mats.iron, [0, 0.55, 0]);
  const shaft = addMesh(group, reg.geo(new THREE.CylinderGeometry(0.13, 0.21, 1.72, 20)), mats.iron, [0, 1.48, 0]);
  shaft.castShadow = true;
  addMesh(group, reg.geo(new THREE.CylinderGeometry(0.2, 0.15, 0.1, 20)), mats.steel, [0, 2.36, 0]);
  addMesh(group, reg.geo(new THREE.BoxGeometry(0.36, 0.09, 0.3)), mats.steel, [0, 2.43, 0]);
  addMesh(group, reg.geo(new THREE.BoxGeometry(0.16, 0.05, 0.24)), mats.agate, [0, 2.475, 0]);
  return group;
}

/** Triangular truss beam, knife edges at both ends, pointer below the pivot. */
function buildBeam(reg, mats) {
  const beam = new THREE.Group();
  const chord = addMesh(beam, reg.geo(new THREE.BoxGeometry(BEAM_HALF * 2, 0.055, 0.055)), mats.brass);
  chord.castShadow = true;

  addMesh(beam, reg.geo(new THREE.BoxGeometry(0.055, 0.32, 0.055)), mats.brass, [0, 0.16, 0]);

  const diagonalLength = Math.hypot(BEAM_HALF, 0.3);
  const diagonal = reg.geo(new THREE.CylinderGeometry(0.022, 0.022, diagonalLength, 8));
  [-1, 1].forEach((side) => {
    const rod = addMesh(beam, diagonal, mats.brass, [side * BEAM_HALF * 0.5, 0.15, 0]);
    rod.rotation.z = side * (Math.PI / 2 - Math.atan2(0.3, BEAM_HALF));
    rod.castShadow = true;
  });

  const post = reg.geo(new THREE.CylinderGeometry(0.016, 0.016, 0.16, 6));
  [-0.81, 0.81].forEach((x) => addMesh(beam, post, mats.brass, [x, 0.075, 0]));

  const knife = reg.geo(new THREE.BoxGeometry(0.1, 0.07, 0.2));
  [-1, 1].forEach((side) => addMesh(beam, knife, mats.steel, [side * BEAM_HALF, -0.03, 0]));

  addMesh(beam, reg.geo(new THREE.BoxGeometry(0.2, 0.14, 0.16)), mats.brassDark, [0, -0.02, 0]);

  const pointer = addMesh(
    beam,
    reg.geo(new THREE.CylinderGeometry(0.012, 0.034, POINTER_LEN, 8)),
    mats.polished,
    [0, -POINTER_LEN / 2 - 0.06, 0.07]
  );
  pointer.castShadow = true;

  const tip = new THREE.Object3D();
  tip.position.set(0, -POINTER_LEN - 0.06, 0.07);
  beam.add(tip);

  return { beam, tip };
}

/** Stirrup hanger and polished pan. Kept vertical against the beam each frame. */
function buildPan(reg, mats) {
  const stirrup = new THREE.Group();
  const rod = reg.geo(new THREE.CylinderGeometry(0.014, 0.014, 0.64, 6));
  [[-0.33, 0.12, 0.14], [0.33, 0.12, 0.14], [0, 0.12, -0.3]].forEach(([x, , z]) => {
    const mesh = addMesh(stirrup, rod, mats.steel, [x * 0.5, -0.34, z * 0.5]);
    mesh.rotation.z = Math.atan2(x * 0.5, 0.64);
    mesh.rotation.x = -Math.atan2(z * 0.5, 0.64);
  });
  addMesh(stirrup, reg.geo(new THREE.TorusGeometry(0.05, 0.012, 6, 14)), mats.steel, [0, -0.02, 0], [Math.PI / 2, 0, 0]);

  const dish = reg.geo(new THREE.LatheGeometry([
    new THREE.Vector2(0.001, 0.0),
    new THREE.Vector2(0.18, 0.012),
    new THREE.Vector2(0.34, 0.038),
    new THREE.Vector2(0.44, 0.078),
    new THREE.Vector2(0.46, 0.096),
    new THREE.Vector2(0.455, 0.086),
    new THREE.Vector2(0.43, 0.072),
    new THREE.Vector2(0.32, 0.03),
    new THREE.Vector2(0.16, 0.006),
    new THREE.Vector2(0.001, -0.004)
  ], 34));
  const pan = addMesh(stirrup, dish, mats.brass, [0, -0.68, 0]);
  pan.castShadow = true;
  addMesh(stirrup, reg.geo(new THREE.TorusGeometry(0.455, 0.014, 6, 30)), mats.brassDark, [0, -0.598, 0], [Math.PI / 2, 0, 0]);

  return { stirrup, pan, dish };
}

/** Instrument scale: an arc of ticks centred on the pivot. Uncoloured. */
function buildScale(reg, mats) {
  const group = new THREE.Group();
  const radius = SCALE_RADIUS;
  const tick = reg.geo(new THREE.BoxGeometry(0.016, 0.085, 0.012));
  const ticks = new THREE.InstancedMesh(tick, mats.polished, 23);
  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  const position = new THREE.Vector3();
  for (let i = 0; i < 23; i += 1) {
    const step = i - 11;
    const angle = step * 1 * DEG;
    const major = step % 5 === 0;
    position.set(Math.sin(angle) * radius, PIVOT_Y - Math.cos(angle) * radius, 0);
    quaternion.setFromAxisAngle(new THREE.Vector3(0, 0, 1), angle);
    scale.set(1, major ? 2.1 : 1, 1);
    matrix.compose(position, quaternion, scale);
    ticks.setMatrixAt(i, matrix);
  }
  ticks.instanceMatrix.needsUpdate = true;
  group.add(ticks);

  const plate = addMesh(group, reg.geo(new THREE.BoxGeometry(0.72, 0.055, 0.02)), mats.iron, [0, PIVOT_Y - radius - 0.1, -0.005]);
  plate.receiveShadow = true;
  group.position.z = 0.14;
  return { group, ticks };
}

/** Arrest fork: rises to clamp the beam, drops to release it. */
function buildArrest(reg, mats) {
  const group = new THREE.Group();
  const prong = reg.geo(new THREE.BoxGeometry(0.045, 0.62, 0.045));
  [-0.19, 0.19].forEach((x) => addMesh(group, prong, mats.steel, [x, PIVOT_Y - 0.44, -0.16]));
  addMesh(group, reg.geo(new THREE.BoxGeometry(0.46, 0.05, 0.05)), mats.steel, [0, PIVOT_Y - 0.14, -0.16]);
  addMesh(group, reg.geo(new THREE.CylinderGeometry(0.03, 0.03, 1.5, 8)), mats.steel, [0, 1.3, -0.16]);
  const lever = addMesh(group, reg.geo(new THREE.CylinderGeometry(0.024, 0.024, 0.62, 8)), mats.iron, [0.3, 0.62, -0.16], [0, 0, Math.PI / 2 - 0.24]);
  lever.castShadow = true;
  addMesh(group, reg.geo(new THREE.SphereGeometry(0.055, 10, 8)), mats.iron, [0.6, 0.55, -0.16]);
  return group;
}

/**
 * The luminaire. A hooded projector on a counterweighted boom: polished
 * reflector, ground-glass lens, an eight-leaf mechanical iris, and a
 * three-arm optical spider braced by three chords across the bezel.
 * Seen from the front the spider frames the aperture. It is the only light
 * source in the scene.
 */
function buildLuminaire(reg, mats) {
  const rig = new THREE.Group();

  addMesh(rig, reg.geo(new THREE.CylinderGeometry(0.56, 0.66, 0.12, 18)), mats.iron, [3.05, 0.06, -2.75]);
  const stand = addMesh(rig, reg.geo(new THREE.CylinderGeometry(0.075, 0.1, 3.3, 14)), mats.iron, [3.05, 1.72, -2.75]);
  stand.castShadow = true;
  addMesh(rig, reg.geo(new THREE.CylinderGeometry(0.11, 0.11, 0.18, 14)), mats.steel, [3.05, 3.36, -2.75]);

  const boomFrom = new THREE.Vector3(3.05, 3.38, -2.75);
  // The head has to sit over the pivot, not off to one side. Mounted off
  // centre, the near pan is always the brighter one whatever the head is
  // aimed at, and the gaze stops meaning anything.
  const boomTo = new THREE.Vector3(0.05, 3.86, -2.25);
  const boomVec = new THREE.Vector3().subVectors(boomTo, boomFrom);
  const boom = addMesh(rig, reg.geo(new THREE.CylinderGeometry(0.055, 0.055, boomVec.length(), 12)), mats.iron);
  boom.position.copy(boomFrom).addScaledVector(boomVec, 0.5);
  boom.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), boomVec.clone().normalize());
  boom.castShadow = true;

  const tail = boomFrom.clone().addScaledVector(boomVec.clone().normalize(), -0.5);
  addMesh(rig, reg.geo(new THREE.CylinderGeometry(0.17, 0.17, 0.3, 14)), mats.iron, [tail.x, tail.y, tail.z], [Math.PI / 2, 0, 0]);

  const yoke = new THREE.Group();
  yoke.position.copy(boomTo);
  rig.add(yoke);
  const arm = reg.geo(new THREE.BoxGeometry(0.045, 0.5, 0.045));
  [-0.4, 0.4].forEach((x) => addMesh(yoke, arm, mats.steel, [x, -0.2, 0]));
  addMesh(yoke, reg.geo(new THREE.BoxGeometry(0.86, 0.05, 0.05)), mats.steel, [0, 0.02, 0]);

  // The head is built facing -Z, but Object3D.lookAt() on a plain Group aims
  // +Z at the target (three.js swaps the arguments for anything that is not a
  // camera or a light). So the gimbal does the aiming and the head hangs off it
  // flipped — that way lookAt() points the lens, not the back of the hood.
  const gimbal = new THREE.Group();
  gimbal.position.set(0, -0.42, 0);
  yoke.add(gimbal);

  const head = new THREE.Group();
  head.rotation.y = Math.PI;
  gimbal.add(head);

  const hood = addMesh(head, reg.geo(new THREE.LatheGeometry([
    new THREE.Vector2(0.3, 0.34),
    new THREE.Vector2(0.38, 0.22),
    new THREE.Vector2(0.5, 0.02),
    new THREE.Vector2(0.6, -0.2),
    new THREE.Vector2(0.62, -0.3)
  ], 32)), mats.iron, [0, 0, 0], [Math.PI / 2, 0, 0]);
  hood.castShadow = true;
  addMesh(head, reg.geo(new THREE.LatheGeometry([
    new THREE.Vector2(0.06, 0.3),
    new THREE.Vector2(0.34, 0.06),
    new THREE.Vector2(0.5, -0.14),
    new THREE.Vector2(0.56, -0.26)
  ], 32)), mats.reflector, [0, 0, 0], [Math.PI / 2, 0, 0]);

  // Brow / visor: an open half-cylinder shading the top of the aperture.
  // After the +90° X rotation the cylinder's theta origin points down, so the
  // arc is centred on PI to sit over the top of the mouth.
  addMesh(head, reg.geo(new THREE.CylinderGeometry(0.645, 0.625, 0.22, 30, 1, true, Math.PI * 0.6, Math.PI * 0.8)), mats.iron, [0, 0, -0.4], [Math.PI / 2, 0, 0]);

  addMesh(head, reg.geo(new THREE.TorusGeometry(0.6, 0.035, 8, 34)), mats.steel, [0, 0, -0.3]);
  addMesh(head, reg.geo(new THREE.TorusGeometry(0.48, 0.026, 8, 30)), mats.brassDark, [0, 0, -0.315]);

  // Optical spider: three radial arms, three chords bracing their tips.
  const spiderArm = reg.geo(new THREE.BoxGeometry(0.036, 0.29, 0.036));
  const chordLength = 0.585 * Math.sqrt(3);
  const spiderChord = reg.geo(new THREE.BoxGeometry(0.03, chordLength, 0.03));
  for (let i = 0; i < 3; i += 1) {
    const angle = Math.PI / 2 + i * (Math.PI * 2 / 3);
    const mid = 0.44;
    addMesh(head, spiderArm, mats.steel,
      [Math.cos(angle) * mid, Math.sin(angle) * mid, -0.32],
      [0, 0, angle - Math.PI / 2]);
    const chordAngle = angle + Math.PI / 3;
    addMesh(head, spiderChord, mats.steel,
      [Math.cos(chordAngle) * 0.2925, Math.sin(chordAngle) * 0.2925, -0.325],
      [0, 0, chordAngle]);
  }

  // Ground glass over the reflector. It stays full size — the iris in front of
  // it is what changes the clear aperture, so this is the pupil behind a lid.
  const lensMaterial = reg.mat(new THREE.MeshStandardMaterial({
    color: 0x12140f,
    emissive: PALETTE.violet,
    emissiveIntensity: 2.4,
    roughness: 0.42,
    metalness: 0
  }));
  const lens = addMesh(head, reg.geo(new THREE.CircleGeometry(0.465, 40)), lensMaterial, [0, 0, -0.3], [0, Math.PI, 0]);

  // Eight-leaf iris. Each leaf pivots on the bezel ring; rotating all of them
  // by the same angle changes the clear aperture.
  const leafGeometry = reg.geo(new THREE.BoxGeometry(0.48, 0.13, 0.008));
  const leafMaterial = reg.mat(new THREE.MeshStandardMaterial({ color: 0x242722, roughness: 0.38, metalness: 0.78, side: THREE.DoubleSide }));
  const leaves = [];
  for (let i = 0; i < 8; i += 1) {
    const base = (i / 8) * Math.PI * 2;
    const pivot = new THREE.Group();
    pivot.position.set(Math.cos(base) * 0.46, Math.sin(base) * 0.46, -0.336 - (i % 2) * 0.006);
    pivot.userData.base = base;
    addMesh(pivot, leafGeometry, leafMaterial, [-0.235, 0, 0]);
    head.add(pivot);
    leaves.push(pivot);
  }
  addMesh(head, reg.geo(new THREE.TorusGeometry(0.55, 0.022, 6, 36)), mats.brassDark, [0, 0, -0.352]);

  const filamentMaterial = reg.mat(new THREE.MeshBasicMaterial({ color: PALETTE.bone, transparent: true, opacity: 0.9 }));
  const filament = addMesh(head, reg.geo(new THREE.SphereGeometry(0.055, 12, 10)), filamentMaterial, [0, 0, 0.02]);

  const spot = new THREE.SpotLight(PALETTE.violet, MODES.neutral.intensity, 16, 0.62, 0.55, 1.4);
  spot.castShadow = true;
  spot.shadow.mapSize.set(1024, 1024);
  spot.shadow.camera.near = 0.4;
  spot.shadow.camera.far = 12;
  spot.shadow.bias = -0.0016;
  spot.shadow.normalBias = 0.02;
  head.add(spot);
  const spotTarget = new THREE.Object3D();
  spotTarget.position.set(0, 0, -1);
  head.add(spotTarget);
  spot.target = spotTarget;

  const glow = new THREE.PointLight(PALETTE.violet, GLOW_BASE, 1.5, 2);
  glow.position.set(0, 0, -0.34);
  head.add(glow);

  return { rig, gimbal, head, leaves, lens, lensMaterial, filamentMaterial, spot, glow };
}

/** Speed trail drawn by the pointer tip. Only visible while the beam moves. */
function buildTrail(reg) {
  const positions = new Float32Array(TRAIL_SAMPLES * 2 * 3);
  const colors = new Float32Array(TRAIL_SAMPLES * 2 * 4);
  const indices = [];
  for (let i = 0; i < TRAIL_SAMPLES - 1; i += 1) {
    const a = i * 2;
    indices.push(a, a + 1, a + 2, a + 1, a + 3, a + 2);
  }
  const geometry = reg.geo(new THREE.BufferGeometry());
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 4));
  geometry.setIndex(indices);
  const material = reg.mat(new THREE.MeshBasicMaterial({
    vertexColors: true,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide
  }));
  const mesh = new THREE.Mesh(geometry, material);
  mesh.frustumCulled = false;
  mesh.renderOrder = 3;
  mesh.userData.noBounds = true;
  return { mesh, geometry, positions, colors, history: [] };
}

/**
 * Bounding box of the solid machine only. The light cone, the pointer trail
 * and the pan afterimages are light, not matter — including them would make
 * the rig claim screen space it does not physically occupy, and the layout
 * pass uses this box to keep clear of the command column.
 */
function solidBounds(root, target = new THREE.Box3()) {
  target.makeEmpty();
  const box = new THREE.Box3();
  root.updateWorldMatrix(true, true);
  root.traverse((object) => {
    if (!object.geometry || object.userData.noBounds) return;
    if (!object.geometry.boundingBox) object.geometry.computeBoundingBox();
    box.copy(object.geometry.boundingBox).applyMatrix4(object.matrixWorld);
    target.union(box);
  });
  return target;
}

function buildRig(reg) {
  const rig = new THREE.Group();
  const frame = new THREE.Group();
  frame.rotation.y = -0.22;
  rig.add(frame);
  const mats = buildMaterials(reg);

  const floor = addMesh(rig, reg.geo(new THREE.PlaneGeometry(22, 13)), mats.floor, [0.4, 0, -3.6], [-Math.PI / 2, 0, 0]);
  floor.receiveShadow = true;
  floor.userData.noBounds = true;

  frame.add(buildPlinth(reg, mats));
  frame.add(buildColumn(reg, mats));

  const scale = buildScale(reg, mats);
  frame.add(scale.group);

  const arrest = buildArrest(reg, mats);
  frame.add(arrest);

  const pivot = new THREE.Group();
  pivot.position.set(0, PIVOT_Y, 0);
  frame.add(pivot);

  const { beam, tip } = buildBeam(reg, mats);
  pivot.add(beam);

  const pans = [-1, 1].map((side) => {
    const end = new THREE.Group();
    end.position.set(side * BEAM_HALF, -0.05, 0);
    beam.add(end);
    const { stirrup, pan, dish } = buildPan(reg, mats);
    end.add(stirrup);

    const ghostMaterial = reg.mat(new THREE.MeshBasicMaterial({
      color: PALETTE.bone,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false
    }));
    const ghosts = [];
    for (let i = 0; i < GHOSTS; i += 1) {
      const ghost = new THREE.Mesh(dish, ghostMaterial);
      ghost.matrixAutoUpdate = false;
      ghost.frustumCulled = false;
      ghost.renderOrder = 1;
      ghost.userData.noBounds = true;
      frame.add(ghost);
      ghosts.push(ghost);
    }
    return { side, end, stirrup, pan, ghosts, ghostMaterial, history: [], swing: new Body({ omega: 11, zeta: 0.16 }) };
  });

  const luminaire = buildLuminaire(reg, mats);
  frame.add(luminaire.rig);

  const trail = buildTrail(reg);
  frame.add(trail.mesh);

  // Measured while the rig group is still identity, so this is the machine's
  // natural extent in rig-local units.
  const bounds = solidBounds(frame);

  return { rig, frame, beam, pivot, tip, pans, scale, arrest, luminaire, trail, mats, bounds };
}

export function initNightwatchScene(canvas) {
  if (!canvas || !supportsWebGL()) {
    canvas?.classList.add("three-unavailable");
    return { setMode() {}, charge() {}, flash() {}, destroy() {} };
  }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
  } catch {
    canvas.classList.add("three-unavailable");
    return { setMode() {}, charge() {}, flash() {}, destroy() {} };
  }

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const coarse = window.matchMedia("(max-width: 700px)");

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.setClearColor(PALETTE.black, 0);
  renderer.shadowMap.enabled = !coarse.matches;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(PALETTE.black, 0.115);
  const ENV_BASE = 2.05;
  const environment = buildEnvironment(renderer);
  scene.environment = environment;
  scene.environmentIntensity = ENV_BASE;

  const CAMERA_Z = 12;
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 60);
  camera.position.set(0, 0, CAMERA_Z);
  camera.lookAt(0, 0, 0);

  // Everything colourful comes from the luminaire. These two only keep the
  // machine from going to pure silhouette outside the pool of light.
  const AMBIENT_BASE = 0.5;
  const FILL_BASE = 0.85;
  const EXPOSURE_BASE = renderer.toneMappingExposure;
  const ambient = new THREE.AmbientLight(PALETTE.bone, AMBIENT_BASE);
  scene.add(ambient);
  const fill = new THREE.DirectionalLight(PALETTE.violet, FILL_BASE);
  fill.position.set(-6, 2.2, 5);
  scene.add(fill);

  const reg = createRegistry();
  const rig = buildRig(reg);
  scene.add(rig.rig);

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  composer.addPass(new EffectPass(camera, new BloomEffect({ intensity: 0.4, luminanceThreshold: 0.74, luminanceSmoothing: 0.3, mipmapBlur: true })));
  composer.addPass(new EffectPass(camera, new VignetteEffect({ darkness: 0.6, offset: 0.28 })));

  const beamBody = new Body({
    omega: BEAM.omega,
    zeta: BEAM.zeta,
    min: -BEAM.stop,
    max: BEAM.stop,
    restitution: BEAM.restitution
  });
  beamBody.locked = true;

  const clock = new THREE.Clock();
  const tmpMatrix = new THREE.Matrix4();
  const tmpInverse = new THREE.Matrix4();
  const tmpBox = new THREE.Box3();
  const tipLocal = new THREE.Vector3();
  const gazePoint = new THREE.Vector3();
  const gazeWorld = new THREE.Vector3();
  const worldA = new THREE.Vector3();
  const worldB = new THREE.Vector3();
  const lampColor = new THREE.Color(MODES.neutral.color);

  // Minimum gap the rig must keep below the command column.
  const CLEARANCE = 24;

  let raf = 0;
  let visible = document.visibilityState === "visible";
  let currentMode = "neutral";
  let gaze = 0;
  let aperture = MODES.neutral.aperture;
  let apertureTarget = MODES.neutral.aperture;
  // 長押しチャージ中のランプ増幅。render ループが毎フレーム掛ける。
  const chargeBoost = { v: 1 };
  let arrestTimer = null;
  let releaseTimer = null;
  let layoutDirty = true;
  let layout = { scale: 1, cropped: false };
  let sigilActive = false;
  let sigilTimer = null;
  let holdTimer = null;
  let typed = "";

  function screenToWorld(px, py, out) {
    const vw = Math.max(1, window.innerWidth);
    const vh = Math.max(1, window.innerHeight);
    const visibleHeight = 2 * CAMERA_Z * Math.tan((camera.fov * DEG) / 2);
    const visibleWidth = visibleHeight * (vw / vh);
    out.set((px / vw - 0.5) * visibleWidth, (0.5 - py / vh) * visibleHeight, 0);
    return out;
  }

  /** Screen-space AABB of a solid subtree, in CSS pixels. */
  function projectedBounds(root = rig.frame) {
    const vw = Math.max(1, window.innerWidth);
    const vh = Math.max(1, window.innerHeight);
    const box = solidBounds(root, tmpBox);
    const point = new THREE.Vector3();
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;
    for (let i = 0; i < 8; i += 1) {
      point.set(
        i & 1 ? box.max.x : box.min.x,
        i & 2 ? box.max.y : box.min.y,
        i & 4 ? box.max.z : box.min.z
      ).project(camera);
      const x = (point.x * 0.5 + 0.5) * vw;
      const y = (0.5 - point.y * 0.5) * vh;
      left = Math.min(left, x);
      right = Math.max(right, x);
      top = Math.min(top, y);
      bottom = Math.max(bottom, y);
    }
    return { left, right, top, bottom };
  }

  function nudge(dxPx, dyPx) {
    screenToWorld(0, 0, worldA);
    screenToWorld(dxPx, dyPx, worldB);
    rig.rig.position.x += worldB.x - worldA.x;
    rig.rig.position.y += worldB.y - worldA.y;
  }

  /**
   * Place the rig against measured DOM geometry. The command column owns the
   * centre of the screen; the rig may only occupy the band below it, and when
   * no band is free it is cropped off the bottom edge rather than shrunk into
   * illegibility.
   *
   * The rig has real depth, so a z=0 pixel estimate under-reports its
   * projected size. Rather than trust the estimate, fit it: place, measure the
   * actual projection, correct, repeat. Convergence takes two or three passes.
   */
  function computeLayout() {
    const vw = Math.max(1, window.innerWidth);
    const vh = Math.max(1, window.innerHeight);
    const shell = document.querySelector(".app-shell");
    const contentBottom = shell ? shell.offsetTop + shell.offsetHeight : vh * 0.72;
    const band = vh - contentBottom;
    const cropped = band < 260;

    // Three framings, all of them fitted by measurement:
    //  - a free band below the console: stand the machine on the band's floor
    //    and fit it to the band's height;
    //  - no free band (a phone): fit to the screen's width and hang it from a
    //    top line, so the beam and the luminaire stay legible while the plinth
    //    runs off the bottom edge;
    //  - the sigil: frame the luminaire head alone, centred in whatever space
    //    the console leaves. The rest of the machine is unlit by then.
    const focus = sigilActive ? rig.luminaire.head : rig.frame;
    const fitWidth = cropped && !sigilActive;
    const centrePx = vw * (sigilActive ? 0.5 : cropped ? 0.52 : 0.5);
    const topLimitPx = contentBottom + CLEARANCE;

    let anchorPx;
    let anchorEdge;
    let targetPx;
    if (sigilActive) {
      anchorEdge = "middle";
      anchorPx = cropped ? vh * 0.7 : (topLimitPx + vh) * 0.5;
      targetPx = cropped
        ? Math.min(vw * 0.54, vh * 0.34)
        : Math.min(vw * 0.4, (vh - topLimitPx) * 0.74, 380);
    } else if (cropped) {
      anchorEdge = "top";
      anchorPx = vh * 0.42;
      targetPx = vw * 1.04;
    } else {
      anchorEdge = "bottom";
      anchorPx = vh - Math.min(26, band * 0.06);
      targetPx = Math.min(
        Math.max(160, anchorPx - topLimitPx),
        THREE.MathUtils.clamp(band * 0.94, 260, 560)
      );
    }

    const visibleHeight = 2 * CAMERA_Z * Math.tan((camera.fov * DEG) / 2);
    const natural = fitWidth
      ? (rig.bounds.max.x - rig.bounds.min.x)
      : (rig.bounds.max.y - rig.bounds.min.y);
    let scale = (targetPx / vh) * visibleHeight / natural;

    const edgeOf = (seen) => (anchorEdge === "top" ? seen.top
      : anchorEdge === "bottom" ? seen.bottom
        : (seen.top + seen.bottom) * 0.5);

    for (let pass = 0; pass < 6; pass += 1) {
      rig.rig.scale.setScalar(scale);
      screenToWorld(centrePx, anchorPx, worldA);
      rig.rig.position.set(
        worldA.x - (rig.bounds.min.x + rig.bounds.max.x) * 0.5 * scale,
        worldA.y - (anchorEdge === "top" ? rig.bounds.max.y : rig.bounds.min.y) * scale,
        0
      );
      for (let fix = 0; fix < 4; fix += 1) {
        const seen = projectedBounds(focus);
        const dx = centrePx - (seen.left + seen.right) * 0.5;
        const dy = anchorPx - edgeOf(seen);
        if (Math.abs(dx) < 0.4 && Math.abs(dy) < 0.4) break;
        nudge(dx, dy);
      }
      const seen = projectedBounds(focus);
      const span = fitWidth ? seen.right - seen.left : seen.bottom - seen.top;
      if (span < 1) break;
      const correction = targetPx / span;
      if (Math.abs(correction - 1) < 0.006) break;
      scale *= correction;
    }

    // Hard clamp. The loop above converges, but the console's height depends
    // on webfont metrics, so a layout can be computed against a stale measure.
    // Rather than trust convergence, shrink until the rig provably clears the
    // console — the one rule this scene is not allowed to break.
    if (!sigilActive && anchorEdge === "bottom") {
      for (let guard = 0; guard < 3; guard += 1) {
        const seen = projectedBounds(focus);
        if (seen.top >= topLimitPx - 0.5) break;
        const room = anchorPx - topLimitPx;
        const height = seen.bottom - seen.top;
        if (height < 1 || room < 1) break;
        scale *= room / height;
        rig.rig.scale.setScalar(scale);
        for (let fix = 0; fix < 4; fix += 1) {
          const now = projectedBounds(focus);
          const dx = centrePx - (now.left + now.right) * 0.5;
          const dy = anchorPx - now.bottom;
          if (Math.abs(dx) < 0.4 && Math.abs(dy) < 0.4) break;
          nudge(dx, dy);
        }
      }
    }

    const seen = projectedBounds(focus);
    layout = {
      scale,
      cropped,
      band,
      contentBottom,
      targetPx,
      measured: { top: seen.top, bottom: seen.bottom, left: seen.left, right: seen.right },
      clearance: seen.top - contentBottom
    };
  }

  function resize() {
    const vw = Math.max(1, window.innerWidth);
    const vh = Math.max(1, window.innerHeight);
    renderer.setSize(vw, vh, false);
    composer.setSize(vw, vh);
    camera.aspect = vw / vh;
    camera.fov = vw / vh < 0.9 ? 34 : 30;
    camera.updateProjectionMatrix();
    renderer.shadowMap.enabled = !coarse.matches;
    layoutDirty = true;
  }

  function setIris(value) {
    const opening = value * IRIS_SWING;
    rig.luminaire.leaves.forEach((leaf) => { leaf.rotation.z = leaf.userData.base + opening; });
  }

  function updateTrail(speed) {
    const { trail } = rig;
    rig.tip.updateWorldMatrix(true, false);
    tmpInverse.copy(rig.frame.matrixWorld).invert();
    tipLocal.setFromMatrixPosition(rig.tip.matrixWorld).applyMatrix4(tmpInverse);
    trail.history.unshift(tipLocal.clone());
    if (trail.history.length > TRAIL_SAMPLES) trail.history.length = TRAIL_SAMPLES;
    while (trail.history.length < TRAIL_SAMPLES) trail.history.push(tipLocal.clone());

    const strength = THREE.MathUtils.clamp((speed - 0.06) / 0.55, 0, 1);
    trail.mesh.visible = strength > 0.01;
    if (!trail.mesh.visible) return;

    const { positions, colors, history } = trail;
    for (let i = 0; i < TRAIL_SAMPLES; i += 1) {
      const point = history[i];
      const next = history[Math.min(i + 1, TRAIL_SAMPLES - 1)];
      let tx = point.x - next.x;
      let ty = point.y - next.y;
      const length = Math.hypot(tx, ty) || 1;
      tx /= length;
      ty /= length;
      const fade = 1 - i / TRAIL_SAMPLES;
      const width = 0.006 + fade * fade * 0.028;
      const a = i * 6;
      positions[a] = point.x - ty * width;
      positions[a + 1] = point.y + tx * width;
      positions[a + 2] = point.z;
      positions[a + 3] = point.x + ty * width;
      positions[a + 4] = point.y - tx * width;
      positions[a + 5] = point.z;
      const alpha = Math.pow(fade, 1.7) * strength * 0.85;
      const c = i * 8;
      for (let v = 0; v < 2; v += 1) {
        colors[c + v * 4] = lampColor.r;
        colors[c + v * 4 + 1] = lampColor.g;
        colors[c + v * 4 + 2] = lampColor.b;
        colors[c + v * 4 + 3] = alpha;
      }
    }
    trail.geometry.attributes.position.needsUpdate = true;
    trail.geometry.attributes.color.needsUpdate = true;
  }

  function updateGhosts(pan, speed) {
    rig.frame.updateWorldMatrix(true, false);
    tmpInverse.copy(rig.frame.matrixWorld).invert();
    pan.pan.updateWorldMatrix(true, false);
    tmpMatrix.multiplyMatrices(tmpInverse, pan.pan.matrixWorld);
    pan.history.unshift(tmpMatrix.clone());
    if (pan.history.length > GHOSTS * 3) pan.history.length = GHOSTS * 3;
    const strength = THREE.MathUtils.clamp((speed - 0.09) / 0.6, 0, 1);
    pan.ghostMaterial.opacity = strength * 0.16;
    pan.ghosts.forEach((ghost, index) => {
      const sample = pan.history[Math.min((index + 1) * 3, pan.history.length - 1)];
      if (sample) ghost.matrix.copy(sample);
      ghost.visible = strength > 0.02;
    });
  }

  function render() {
    if (!visible) return;
    const dt = Math.min(clock.getDelta(), 1 / 24);
    const still = reducedMotion.matches;

    if (still) {
      beamBody.value = beamBody.target;
      beamBody.velocity = 0;
      beamBody.accel = 0;
    } else {
      beamBody.step(dt);
    }
    rig.beam.rotation.z = beamBody.value;

    const speed = Math.abs(beamBody.velocity);

    rig.pans.forEach((pan) => {
      // Stirrups hang vertically. Their small residual sway is forced by the
      // beam's angular acceleration and outlives the beam's own motion.
      if (still) {
        pan.swing.value = 0;
      } else {
        pan.swing.target = THREE.MathUtils.clamp(-beamBody.accel * 0.0055 * pan.side, -0.05, 0.05);
        pan.swing.step(dt);
      }
      pan.stirrup.rotation.z = -beamBody.value + pan.swing.value;
      if (still) {
        pan.ghosts.forEach((ghost) => { ghost.visible = false; });
      } else {
        updateGhosts(pan, speed);
      }
    });

    if (still) {
      rig.trail.mesh.visible = false;
    } else {
      updateTrail(speed);
    }

    // The head follows the pan that came up, with a heavy lag — unless the
    // sigil is up, in which case it stops watching the balance and watches you.
    const gazeTarget = beamBody.value === 0 ? 0 : THREE.MathUtils.clamp(beamBody.value / BEAM.tilt, -1, 1);
    gaze = still ? gazeTarget : THREE.MathUtils.damp(gaze, gazeTarget, 3.2, dt);
    rig.frame.updateWorldMatrix(true, true);
    if (sigilActive) {
      gazeWorld.copy(camera.position);
    } else {
      // The head turns onto the pan that rose toward it, and lifts its aim a
      // little to follow it up. It never turns so far that the other pan
      // leaves the pool — a balance you can only see half of stops being one.
      gazePoint.set(gaze * BEAM_HALF * 0.82, PIVOT_Y - 0.78 + Math.abs(gaze) * 0.1, 0.25);
      gazeWorld.copy(gazePoint).applyMatrix4(rig.frame.matrixWorld);
    }
    rig.luminaire.gimbal.lookAt(gazeWorld);

    aperture = still ? apertureTarget : THREE.MathUtils.damp(aperture, apertureTarget, 4.4, dt);
    setIris(aperture);

    // Filament flicker is the only motion left once the beam has stopped, and
    // it is the one thing reduced motion switches off. The lamp's brightness
    // still has to track the mode either way.
    const t = clock.elapsedTime;
    const flicker = still ? 1 : 0.94 + Math.sin(t * 2.3) * 0.035 + Math.sin(t * 7.1 + 1.4) * 0.018;
    // Aimed at the viewer the lamp lights nothing, so its throw is pulled back
    // and the glass carries the brightness instead.
    const throwScale = sigilActive ? 0.4 : 1;
    const glassBase = sigilActive ? 3.6 : 0.9;
    rig.luminaire.spot.intensity = MODES[currentMode].intensity * chargeBoost.v * throwScale * (0.35 + aperture * 0.75) * flicker;
    rig.luminaire.filamentMaterial.opacity = 0.55 + aperture * 0.4 * flicker;
    rig.luminaire.lensMaterial.emissiveIntensity = (glassBase + aperture * 1.9) * flicker;

    // Laid out after the head is posed, so the box measured is the box drawn.
    if (layoutDirty) {
      computeLayout();
      layoutDirty = false;
    }

    composer.render();
    raf = window.requestAnimationFrame(render);
  }

  function engageArrest() {
    beamBody.clampTo(0);
    beamBody.locked = true;
    rig.beam.rotation.z = 0;
    gsap.to(rig.arrest.position, { y: 0, duration: 0.26, ease: "power2.out", overwrite: true });
  }

  function setMode(mode = "neutral") {
    const next = MODES[mode] ? mode : "neutral";
    if (sigilActive) setSigil(false);
    if (next === currentMode) return;
    currentMode = next;
    layoutDirty = true;
    const spec = MODES[next];

    window.clearTimeout(arrestTimer);
    window.clearTimeout(releaseTimer);
    arrestTimer = null;
    releaseTimer = null;

    apertureTarget = spec.aperture;

    const color = new THREE.Color(spec.color);
    [lampColor, rig.luminaire.spot.color, rig.luminaire.glow.color,
      rig.luminaire.lensMaterial.emissive,
      rig.luminaire.filamentMaterial.color].forEach((target) => {
      gsap.to(target, { r: color.r, g: color.g, b: color.b, duration: 0.7, ease: "power2.out", overwrite: true });
    });
    rig.pans.forEach((pan) => {
      gsap.to(pan.ghostMaterial.color, { r: color.r, g: color.g, b: color.b, duration: 0.7, ease: "power2.out", overwrite: true });
    });

    if (reducedMotion.matches) {
      beamBody.locked = next === "neutral";
      beamBody.clampTo(spec.tilt);
      rig.beam.rotation.z = spec.tilt;
      rig.arrest.position.y = next === "neutral" ? 0 : -0.12;
      return;
    }

    if (next === "neutral") {
      // Let the beam swing back, then lock it. The arrest is the stop pose.
      beamBody.target = 0;
      const settle = () => {
        if (beamBody.settled(0.35 * DEG, 0.6 * DEG)) engageArrest();
        else arrestTimer = window.setTimeout(settle, 90);
      };
      arrestTimer = window.setTimeout(settle, 220);
      releaseTimer = window.setTimeout(engageArrest, 3600);
      return;
    }

    // The beam cannot move until the arrest fork has dropped clear of it.
    gsap.to(rig.arrest.position, { y: -0.12, duration: 0.22, ease: "power2.in", overwrite: true });
    releaseTimer = window.setTimeout(() => {
      beamBody.locked = false;
      beamBody.target = spec.tilt;
    }, 140);
  }

  const caption = document.createElement("div");
  caption.className = "sigil-caption";
  caption.setAttribute("aria-hidden", "true");
  caption.textContent = SIGIL.caption;
  document.body.appendChild(caption);

  function tintLamp(hex, duration) {
    const color = new THREE.Color(hex);
    [lampColor, rig.luminaire.spot.color, rig.luminaire.glow.color,
      rig.luminaire.lensMaterial.emissive,
      rig.luminaire.filamentMaterial.color].forEach((target) => {
      gsap.to(target, { r: color.r, g: color.g, b: color.b, duration, ease: "power2.out", overwrite: true });
    });
  }

  /**
   * 長押しチャージ。押している間ランプが張り詰め、離すと元に戻る。
   * 発火そのものは呼び出し側(発注ボタン)の責務で、ここは光だけ。
   */
  function charge(active) {
    gsap.to(chargeBoost, {
      v: active ? 1.5 : 1,
      duration: active ? 0.55 : 0.3,
      ease: active ? "power2.in" : "power2.out",
      overwrite: true,
    });
    apertureTarget = active ? 1.0 : MODES[currentMode].aperture;
  }

  /** 一瞬だけ別色に灯してから現在モードの色へ戻す(結果の勝ち負けなど)。 */
  function flash(hex, holdMs = 160) {
    tintLamp(hex, 0.1);
    gsap.to(chargeBoost, { v: 1.7, duration: 0.08, ease: "power1.in", overwrite: true });
    window.setTimeout(() => {
      tintLamp(MODES[currentMode].color, 0.9);
      gsap.to(chargeBoost, { v: 1, duration: 0.9, ease: "power2.out", overwrite: true });
    }, holdMs);
  }

  function setSigil(active) {
    if (active === sigilActive) return;
    sigilActive = active;
    layoutDirty = true;
    window.clearTimeout(sigilTimer);
    sigilTimer = null;

    if (active) {
      // The room falls away and the balance is arrested level. Nothing is
      // being weighed any more — the lamp has stopped looking at the pans.
      window.clearTimeout(arrestTimer);
      window.clearTimeout(releaseTimer);
      arrestTimer = null;
      releaseTimer = null;
      engageArrest();
      apertureTarget = 1;
      tintLamp(SIGIL.color, 0.9);
      // Room lights and the reflected environment both go out, so the machine
      // falls to black and the aperture is the only thing left in the frame:
      // a lit disc with the dark spider triangle laid across it.
      gsap.to(ambient, { intensity: 0.02, duration: 0.9, ease: "power2.out", overwrite: true });
      gsap.to(fill, { intensity: 0.02, duration: 0.9, ease: "power2.out", overwrite: true });
      gsap.to(scene, { environmentIntensity: 0.06, duration: 0.9, ease: "power2.out", overwrite: true });
      gsap.to(rig.luminaire.glow, { intensity: 2.6, duration: 0.9, ease: "power2.out", overwrite: true });
      gsap.to(renderer, { toneMappingExposure: 1.16, duration: 0.9, ease: "power2.out", overwrite: true });
      caption.classList.add("show");
      sigilTimer = window.setTimeout(() => setSigil(false), SIGIL.durationMs);
      return;
    }

    const spec = MODES[currentMode];
    apertureTarget = spec.aperture;
    tintLamp(spec.color, 1.1);
    gsap.to(ambient, { intensity: AMBIENT_BASE, duration: 1.1, ease: "power2.out", overwrite: true });
    gsap.to(fill, { intensity: FILL_BASE, duration: 1.1, ease: "power2.out", overwrite: true });
    gsap.to(scene, { environmentIntensity: ENV_BASE, duration: 1.1, ease: "power2.out", overwrite: true });
    gsap.to(rig.luminaire.glow, { intensity: GLOW_BASE, duration: 1.1, ease: "power2.out", overwrite: true });
    gsap.to(renderer, { toneMappingExposure: EXPOSURE_BASE, duration: 1.1, ease: "power2.out", overwrite: true });
    caption.classList.remove("show");
    if (currentMode !== "neutral") {
      beamBody.locked = false;
      beamBody.target = spec.tilt;
    }
  }

  function onKeyDown(event) {
    if (sigilActive) { setSigil(false); return; }
    if (event.key.length !== 1 || event.metaKey || event.ctrlKey || event.altKey) return;
    typed = (typed + event.key).slice(-SIGIL.sequence.length);
    if (typed === SIGIL.sequence) {
      typed = "";
      setSigil(true);
    }
  }

  const wordmark = document.querySelector(".wordmark") || document.querySelector(".brandlock h1");
  function startHold() {
    window.clearTimeout(holdTimer);
    holdTimer = window.setTimeout(() => setSigil(true), SIGIL.holdMs);
  }
  function cancelHold() {
    window.clearTimeout(holdTimer);
    holdTimer = null;
  }
  function onDismiss() {
    if (sigilActive) setSigil(false);
  }

  function onVisibility() {
    visible = document.visibilityState === "visible";
    if (visible && !raf) {
      clock.start();
      raf = window.requestAnimationFrame(render);
    } else if (!visible && raf) {
      window.cancelAnimationFrame(raf);
      raf = 0;
    }
  }

  function screenBounds() {
    return { ...projectedBounds(), layout };
  }

  resize();
  setIris(aperture);
  rig.arrest.position.y = 0;
  window.addEventListener("resize", resize, { passive: true });
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("pointerdown", onDismiss, { passive: true });
  if (wordmark) {
    wordmark.addEventListener("pointerdown", startHold, { passive: true });
    ["pointerup", "pointerleave", "pointercancel"].forEach((type) => {
      wordmark.addEventListener(type, cancelHold, { passive: true });
    });
  }

  // The console grows and shrinks as previews open, and the rig is placed
  // against its measured bottom edge — so watch it, not just the window.
  const shell = document.querySelector(".app-shell");
  const shellObserver = shell && "ResizeObserver" in window
    ? new ResizeObserver(() => { layoutDirty = true; })
    : null;
  shellObserver?.observe(shell);
  // The blackletter wordmark changes the console's height when it swaps in.
  document.fonts?.ready.then(() => { layoutDirty = true; }).catch(() => {});

  raf = window.requestAnimationFrame(render);

  window.Nightwatch3D = { setMode, screenBounds, charge, flash, mode: () => currentMode };

  return {
    setMode,
    screenBounds,
    charge,
    flash,
    destroy() {
      window.cancelAnimationFrame(raf);
      window.clearTimeout(arrestTimer);
      window.clearTimeout(releaseTimer);
      window.clearTimeout(sigilTimer);
      window.clearTimeout(holdTimer);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onDismiss);
      if (wordmark) {
        wordmark.removeEventListener("pointerdown", startHold);
        ["pointerup", "pointerleave", "pointercancel"].forEach((type) => {
          wordmark.removeEventListener(type, cancelHold);
        });
      }
      caption.remove();
      shellObserver?.disconnect();
      gsap.killTweensOf([rig.arrest.position, lampColor]);
      composer.dispose();
      environment.dispose();
      renderer.dispose();
      reg.dispose();
      delete window.Nightwatch3D;
    }
  };
}
