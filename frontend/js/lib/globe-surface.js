/* The 3D globe's surface: NASA Blue Marble if it is on disk, the drawn
   coastline canvas if it is not.

   Borrowed from SattrackSlop (https://github.com/ColaBear101/SattrackSlop,
   MIT) — its `earth/globetex.js` and the surface half of `earth/orbit3d.js`.
   What came across: the day/night fragment shader and its explicit gamma, the
   texture filtering, the GPU texture-size ceiling, and the rule about when an
   image needs `crossOrigin`. What did NOT come across is the part that makes
   that repository's globe work: it fetches its imagery from NASA GIBS at
   runtime. This station may sit on an isolated LAN, which is the same reason
   there are no webfonts and no CDNs anywhere in this frontend. Everything here
   loads from our own origin or not at all.

   The imagery itself is NASA Blue Marble — U.S. Government work, public
   domain, no attribution required and given anyway. `tools/fetch_vendor.sh`
   puts it at assets/earth-surface.jpg; nothing commits it.

   THE RULE THIS MODULE EXISTS TO ENFORCE: the globe renders with no imagery
   present. The panel used to be CesiumJS, 23 MB fetched by a setup script, and
   because it was fetched rather than committed a clone that skipped that step
   showed a black rectangle with no hint why. Imagery is back, so that trap is
   back — which is why a missing file is not an error here but a branch, and why
   the branch it takes is written into the panel's hint rather than only into
   the console. A blank-looking globe must never again be a mystery. */

/* One filename, whatever resolution is inside it. The obvious design was
   earth-2048.jpg / earth-4096.jpg with a constant here naming the rung, and it
   is wrong: the rung is chosen by whoever runs the fetch script, so the
   constant and the script's default are two places to keep in step and one of
   them is in a language the other cannot read. The resolution is instead read
   off the loaded image and reported, so the panel tells you what it actually
   got rather than what this file expected. `.gitignore` already covers
   assets/earth-*.jpg. */
const SURFACE_URL = '/assets/earth-surface.jpg';

const TEX_W = 2048;                 // the drawn fallback's canvas
const TEX_H = 1024;

/* The night side is not black. The Earth at night is lit by a hemisphere of
   sky and by the Moon, and a pure black limb reads as a hole cut out of the
   panel rather than as a planet. SattrackSlop uses 0.055 against a black page
   and crossfades a city-lights mosaic in over the top; we have no lights map
   (fetching one is exactly what is banned here) so this floor is the whole of
   the night side and is set higher — high enough that the continents and the
   ground track crossing them stay readable from across a room, low enough that
   the terminator is still the first thing you see. */
const NIGHT_FLOOR = 0.13;

/* Sun glint on water, scaled down hard. It is here because it is the one
   ornament that is also a reading: the highlight sits where the Sun is
   specularly reflected, so it says which way the Sun lies even on the half of
   the globe where the terminator has run off the limb. Any higher and it
   becomes a lens flare on an instrument.

   The exponent is the part that needed looking at rather than reasoning about.
   Real glint off water is broad, because the sea is rough — but at 72 the
   highlight came out about eight degrees wide against a globe eighteen degrees
   across, and a soft bright patch that size on a photograph of the Pacific
   reads as weather rather than as sunlight. Tightened until it reads as a
   highlight. */
const GLINT = 0.22;
const GLINT_TIGHTNESS = 200.0;

// --------------------------------------------------------------------------
// image loading
// --------------------------------------------------------------------------

/** Resolve with the decoded image, or reject. Never throws synchronously. */
function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    /* No crossOrigin, deliberately. SattrackSlop sets it only for genuinely
       cross-origin sources and warns against setting it on a relative path:
       a page opened from disk has an opaque origin, the CORS check on a file
       in the next directory fails, and the imagery then never loads for
       anyone. Ours is always same-origin, so asking for CORS could only ever
       break it. */
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('no imagery at ' + src));
    img.src = src;
  });
}

/* The filtering an equirectangular map needs, whether it was drawn here or
   photographed from orbit. Every texel row converges to a point at the poles,
   so the texture is sampled across a hugely stretched footprint there; without
   anisotropy that reads as smeared polar caps at any grazing angle. This panel
   was asking for a flat 4 — the GPU will usually give 16.

   `srgb` is false for anything bound to the day/night shader: three.js only
   decodes a texture's colour space for its own materials, and that shader does
   its own gamma, so marking it would decode it twice and the terminator turns
   to mud. */
function tuneTexture(THREE, renderer, texture, srgb) {
  const caps = renderer?.capabilities;
  if (caps?.getMaxAnisotropy) texture.anisotropy = caps.getMaxAnisotropy();
  texture.generateMipmaps = true;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  texture.magFilter = THREE.LinearFilter;
  if (srgb) texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

// --------------------------------------------------------------------------
// the drawn surface — what runs when there is no imagery
// --------------------------------------------------------------------------

/* Unchanged in substance from the version that lived in globe3d.js: the
   coastlines are painted once into an equirectangular canvas from the same
   public-domain Natural Earth outline the 2D map uses, so the two panels
   cannot disagree about where land is. It is not a degraded mode. It is a
   diagram, it is honest about being one, and it is the reason this panel can
   be cloned and opened with nothing fetched. */
async function drawnTexture(THREE, renderer, colors, landUrl) {
  const canvas = document.createElement('canvas');
  canvas.width = TEX_W;
  canvas.height = TEX_H;
  const g = canvas.getContext('2d');

  g.fillStyle = colors.ocean;
  g.fillRect(0, 0, TEX_W, TEX_H);

  // Graticule first, so coastlines sit on top of it.
  g.strokeStyle = colors.graticule;
  g.lineWidth = 1;
  g.beginPath();
  for (let lon = -180; lon <= 180; lon += 30) {
    const x = (lon + 180) / 360 * TEX_W;
    g.moveTo(x, 0); g.lineTo(x, TEX_H);
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    const y = (90 - lat) / 180 * TEX_H;
    g.moveTo(0, y); g.lineTo(TEX_W, y);
  }
  g.stroke();

  let coastlines = true;
  try {
    const land = await fetch(landUrl).then((r) => r.json());
    g.beginPath();
    for (const feature of land.features || []) {
      const geom = feature.geometry;
      if (!geom) continue;
      const polys = geom.type === 'Polygon' ? [geom.coordinates] : geom.coordinates;
      for (const poly of polys) {
        for (const ring of poly) {
          ring.forEach(([lon, lat], i) => {
            const x = (lon + 180) / 360 * TEX_W;
            const y = (90 - lat) / 180 * TEX_H;
            if (i) g.lineTo(x, y); else g.moveTo(x, y);
          });
          g.closePath();
        }
      }
    }
    g.fillStyle = colors.land;
    g.fill();
    g.strokeStyle = colors.coast;
    g.lineWidth = 1.6;
    g.stroke();
  } catch (err) {
    // A globe with a graticule and no coastlines is still a usable globe.
    console.warn('[globe3d] coastlines unavailable', err);
    coastlines = false;
  }

  return { texture: tuneTexture(THREE, renderer, new THREE.CanvasTexture(canvas), true), coastlines };
}

// --------------------------------------------------------------------------
// the photographic surface
// --------------------------------------------------------------------------

/* One shader rather than a second MeshPhongMaterial, for the night side.

   Phong needs an emissive pass to keep the dark half legible, and emissive
   ADDS: turn it up far enough to see the night hemisphere and the day
   hemisphere lifts with it until the terminator has flattened out. That is
   fine for the drawn surface, whose texture is four flat colours and whose
   whole job is to stay readable — it is how the drawn path still works. It is
   wrong for a photograph, which has its own contrast to protect. Here the
   night side is instead dimmed MULTIPLICATIVELY down to a floor, so the day
   side keeps every stop of the original and the terminator stays the hardest
   edge on the globe.

   The gamma is explicit, and this is the part that is easy to get wrong.
   three.js applies its output colour-space conversion through a shader chunk
   that only its own materials include, so a raw ShaderMaterial's gl_FragColor
   reaches the framebuffer untouched. Lighting has to happen in linear light or
   the terminator goes muddy, so the map is decoded on the way in and the
   result encoded on the way out. Lifted from SattrackSlop, which measured this
   rather than assuming it. */
const DAYNIGHT_VERT = /* glsl */`
  varying vec2 vUv;
  varying vec3 vWN;
  varying vec3 vWP;
  void main() {
    vUv = uv;
    /* The WORLD normal, not the view normal. Nothing spins this globe today —
       the frame is earth-fixed on purpose — but a view-space normal would swing
       the terminator round with the camera, which is a bug that only appears
       once somebody drags. */
    vWN = normalize(mat3(modelMatrix) * normal);
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vWP = wp.xyz;
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`;

const DAYNIGHT_FRAG = /* glsl */`
  uniform sampler2D dayMap;
  uniform vec3 sunDir;
  uniform float nightFloor;
  uniform float glint;
  uniform float glintTightness;
  uniform vec3 glintColor;
  varying vec2 vUv;
  varying vec3 vWN;
  varying vec3 vWP;

  vec3 lin(vec3 c) { return pow(c, vec3(2.2)); }

  void main() {
    vec3 n = normalize(vWN);
    vec3 s = normalize(sunDir);
    float d = dot(n, s);

    vec3 raw = texture2D(dayMap, vUv).rgb;

    /* The ocean mask, read straight out of the photograph. Blue Marble has no
       specular map and we are not going to fetch one, but water is the only
       thing on this image where blue clearly leads red: deep ocean sits around
       b-r = 0.16, vegetated land goes negative, and ice and cloud are neutral
       so they cancel. Taken before the decode, because linear light compresses
       exactly the difference being measured. */
    float sea = clamp((raw.b - raw.r) * 3.5, 0.0, 1.0);

    // Lambert, with a floor. The falloff IS the terminator.
    float k = nightFloor + (1.0 - nightFloor) * clamp(d, 0.0, 1.0);
    vec3 c = lin(raw) * k;

    vec3 h = normalize(s + normalize(cameraPosition - vWP));
    c += glintColor * (glint * sea * clamp(d, 0.0, 1.0)
                       * pow(max(dot(n, h), 0.0), glintTightness));

    gl_FragColor = vec4(pow(c, vec3(1.0 / 2.2)), 1.0);
  }
`;

// --------------------------------------------------------------------------
// atmosphere
// --------------------------------------------------------------------------

/* A back-faced shell brightened at grazing angles: from outside the sphere
   only the ring beyond the planet's silhouette survives, which is what reads
   as air. Additive and depth-write-off so it can only brighten — it must never
   be able to hide the ground track passing in front of it.

   This replaces a flat back-side shell at a constant 10% opacity, which put as
   much haze over the middle of the disc as over the limb and just greyed the
   whole planet down. Fresnel term from SattrackSlop, turned down: theirs is
   tuned for a full-screen globe on a black page, this one is a 200-pixel
   window on white and reads as a fog bank at that strength. */
export function atmosphereMaterial(THREE, tint) {
  return new THREE.ShaderMaterial({
    transparent: true,
    side: THREE.BackSide,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: { tint: { value: new THREE.Color(tint) } },
    vertexShader: /* glsl */`
      varying vec3 vN;
      varying vec3 vP;
      void main() {
        vN = normalize(normalMatrix * normal);
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vP = mv.xyz;
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */`
      uniform vec3 tint;
      varying vec3 vN;
      varying vec3 vP;
      void main() {
        float f = pow(clamp(1.0 - abs(dot(normalize(vN), normalize(-vP))), 0.0, 1.0), 3.0);
        gl_FragColor = vec4(tint, f * 0.5);
      }
    `,
  });
}

// --------------------------------------------------------------------------
// the decision
// --------------------------------------------------------------------------

/** Build the Earth's surface material, imagery if there is any and the drawn
 *  canvas if there is not.
 *
 *  Returns `{ material, kind, hint, detail, lit, setSun(unitVec) }`:
 *    kind    'imagery' | 'drawn'
 *    hint    a few words for the panel heading
 *    detail  a sentence for its tooltip, naming the file and the way back
 *    lit     whether the caller's scene lights do anything (the shader ignores
 *            them; the drawn Phong material needs them)
 */
export async function buildEarthSurface({ THREE, renderer, colors, landUrl }) {
  let img = null;
  let why = null;

  try {
    img = await loadImage(SURFACE_URL);
  } catch (err) {
    /* Not an error. The whole point of this module is that this is a supported
       state of the world, so it is a debug note and a sentence on the panel.
       The browser will still log its own 404 for the image request and there is
       no suppressing that from here — which is no bad thing, since it names the
       exact path that is missing. */
    why = 'no imagery on disk';
    console.debug(`[globe3d] ${err.message} — drawing the coastline surface instead`);
  }

  /* The GPU's own ceiling. A texture wider than MAX_TEXTURE_SIZE is not a
     sharper globe, it is a failed upload and a black sphere — 16384 on a
     discrete card, 8192 on a software renderer, as low as 4096 on some
     phones, so an 8192 map is a real risk and not a theoretical one. The check
     is after the load rather than before because the resolution is whatever is
     in the file, and paying for a local read to find out is cheap next to
     keeping a second copy of the answer in this file. */
  const cap = renderer?.capabilities?.maxTextureSize || 0;
  if (img && cap && img.naturalWidth > cap) {
    why = `imagery is ${img.naturalWidth}px, this GPU takes ${cap}px`;
    console.warn(`[globe3d] ${why} — drawing the coastline surface instead`);
    img = null;
  }

  if (img) {
    const texture = tuneTexture(THREE, renderer, new THREE.Texture(img), false);
    const material = new THREE.ShaderMaterial({
      uniforms: {
        dayMap: { value: texture },
        sunDir: { value: new THREE.Vector3(1, 0, 0) },
        nightFloor: { value: NIGHT_FLOOR },
        glint: { value: GLINT },
        glintTightness: { value: GLINT_TIGHTNESS },
        glintColor: { value: new THREE.Color(colors.glint) },
      },
      vertexShader: DAYNIGHT_VERT,
      fragmentShader: DAYNIGHT_FRAG,
    });
    const px = `${img.naturalWidth}×${img.naturalHeight}`;
    return {
      material,
      kind: 'imagery',
      hint: `Blue Marble ${img.naturalWidth}`,
      detail: `NASA Blue Marble, ${px}, from ${SURFACE_URL} (public domain).`,
      lit: false,
      setSun: (dir) => { material.uniforms.sunDir.value.copy(dir); },
    };
  }

  const { texture, coastlines } = await drawnTexture(THREE, renderer, colors, landUrl);
  const material = new THREE.MeshPhongMaterial({
    map: texture,
    /* The same texture is both map and emissiveMap. The emissive pass is a
       floor: it puts the coastlines on screen regardless of where the Sun is,
       so the night half is still a map rather than a black hole. The
       directional light then adds the day side on top, which is what makes the
       terminator visible at all. Lighting alone gave a black disc — every one
       of these deliberately dark surface colours multiplied down to
       indistinguishable black and the globe rendered as a silhouette with a
       track floating on it. */
    emissive: 0xffffff,
    emissiveMap: texture,
    emissiveIntensity: 0.55,
    shininess: 8,
    specular: new THREE.Color(colors.specular),
  });

  return {
    material,
    kind: 'drawn',
    hint: coastlines ? 'drawn coastlines' : 'graticule only',
    detail: `${why || 'imagery unavailable'} — the surface is drawn from ${landUrl}`
          + ` (Natural Earth, public domain). For NASA Blue Marble imagery run`
          + ` tools/fetch_vendor.sh, which writes ${SURFACE_URL}.`,
    lit: true,
    setSun: null,
  };
}

export { SURFACE_URL };
