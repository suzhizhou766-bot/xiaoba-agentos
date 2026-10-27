import { defineScene } from 'origin-scene'

const W = 1920
const H = 1080
const STEP_HZ = 15
const BACKGROUND = '#111415'

/** Absolute, deterministic sample. No retained random generator or frame-count state. */
/** @param {number} seed @param {number} index */
const noise = (seed, index) => {
  let value = Math.imul((index | 0) ^ (seed | 0), 0x45d9f3b)
  value = Math.imul(value ^ (value >>> 16), 0x45d9f3b)
  return ((value ^ (value >>> 16)) >>> 0) / 4294967296
}
/** @param {number} value */
const clamp = (value) => Math.max(0, Math.min(1, value))
/** @param {number} value */
const signed = (value) => value * 2 - 1

export default defineScene({
  apiVersion: 2,
  background: BACKGROUND,
  async setup({ authoring: a, layers, props }) {
    const title = props.title ?? 'ORIGIN CUT'
    const subtitle = props.subtitle ?? 'ORIGIN STREAM · CREATIVE SIGNAL · CH 03'
    const channel = props.channel ?? 3
    const intensity = props.intensity ?? 0.86
    const seed = props.seed ?? 41
    const showHud = props.showHud ?? true
    const showColorBars = props.showColorBars ?? true
    const accentLeft = props.accentLeft ?? '#ff2a55'
    const accentRight = props.accentRight ?? '#00e6ff'

    // Literal project-root references are captured as immutable font bytes by Check.
    const titleStyle = {
      font: {
        source: /** @type {const} */ ('component-file'),
        path: 'assets/fonts/big-shoulders-display.ttf',
      },
      fontSize: 568,
      fontWeight: 900,
    }
    const monoStyle = {
      font: {
        source: /** @type {const} */ ('component-file'),
        path: 'assets/fonts/martian-mono.ttf',
      },
      fontSize: 43,
      fontWeight: 500,
    }
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 !?._-/:·'
    await a.text.prepare({ text: title + alphabet, style: titleStyle })
    await a.text.prepare({ text: subtitle + alphabet + 'TRACKINGPLAYSPCH', style: monoStyle })
    const titleFont = a.text.canvasFont({ text: title, style: titleStyle })
    const subtitleFont = a.text.canvasFont({ text: subtitle || ' ', style: monoStyle })
    const hudFont = a.text.canvasFont({ text: alphabet, style: { ...monoStyle, fontSize: 29 } })

    /** @param {number} seconds */
    const corruptedGlyph = (seconds) => {
      const slot = Math.floor(seconds * STEP_HZ + 0.000001)
      return intensity > 0.5 && title.includes('N') && (slot === 8 || slot === 24 || slot === 49)
    }

    // Three Host-owned word plates, cached by two glyph states rather than repainted per frame.
    // The occasional N → 0 is one discrete sample; the same surfaces are reused.
    const plates = ['#ffffff', accentLeft, accentRight].map((color, index) =>
      a.surface.canvas2d({
        id: `word-plate-${index}`,
        size: { width: W, height: H },
        cache: { key: (frame) => (corruptedGlyph(frame.time.sourceSeconds) ? 1 : 0) },
        paint({ ctx }, frame) {
          ctx.fillStyle = color
          ctx.textAlign = 'center'
          ctx.textBaseline = 'alphabetic'
          ctx.font = titleFont
          const measured = ctx.measureText(title).width
          ctx.save()
          ctx.translate(958, 639)
          ctx.scale(Math.min(1.28, 1610 / Math.max(1, measured)), 1)
          ctx.fillText(
            corruptedGlyph(frame.time.sourceSeconds) ? title.replace('N', '0') : title,
            0,
            0,
          )
          ctx.restore()
          if (subtitle) {
            ctx.font = subtitleFont
            ctx.fillText(subtitle, W / 2, 775, 1440)
          }
        },
      }),
    )

    // Two bounded, cached grain fields; Source Time selects position and field.
    const grains = [0, 1].map((index) =>
      a.surface.canvas2d({
        id: `grain-field-${index}`,
        size: { width: 960, height: 540 },
        cache: 'static',
        paint({ ctx }) {
          const image = ctx.createImageData(960, 540)
          for (let pixel = 0; pixel < 960 * 540; pixel++) {
            const value = Math.floor(noise(seed + index * 97, pixel + 10000) * 166)
            image.data[pixel * 4] = value
            image.data[pixel * 4 + 1] = value
            image.data[pixel * 4 + 2] = value
            image.data[pixel * 4 + 3] = 255
          }
          ctx.putImageData(image, 0, 0)
        },
      }),
    )

    const timeline = a.motion.timeline({ id: 'signal-interruption' })
    const film = timeline.phase({
      id: 'film',
      label: 'Four-second persistent broadcast word',
      start: 0,
      duration: 4,
    })
    const entry = timeline.phase({
      id: 'entry',
      label: 'Receiver locks onto the existing word',
      parent: film,
      start: 0,
      duration: 0.12,
    })
    const bursts = [
      [0.4, 0.8],
      [1.45, 1.7],
      [2.35, 2.55],
      [3.15, 3.6],
    ]
    const envelopes = bursts.map(([start, end], index) => {
      const phase = timeline.phase({
        id: `tracking-failure-${index + 1}`,
        label: `Tracking failure ${index + 1} / horizontal displacement`,
        parent: film,
        start,
        duration: end - start,
      })
      timeline.cue({ id: `carrier-recovered-${index + 1}`, at: phase.end })
      const nextStart = bursts[index + 1]?.[0] ?? 4
      timeline.phase({
        id: `recovery-${index + 1}`,
        label: `Readable recovery ${index + 1}`,
        parent: film,
        start: phase.end,
        duration: nextStart - end,
      })
      return timeline.signal.envelope({
        id: `burst-envelope-${index + 1}`,
        at: phase.start,
        duration: end - start,
        attack: 0.012,
        release: 0.055,
        amplitude: 1,
      })
    })
    const faultSignal = timeline.signal.clamp(timeline.signal.add(...envelopes), 0, 1)
    const state = a.motion.target(
      { opacity: 0, fault: 0 },
      { id: 'signal-state', adapter: a.motion.adapters.properties(['opacity', 'fault']) },
    )
    timeline.fromTo(state, {
      id: 'receiver-lock',
      label: 'Signal becomes visible',
      phase: entry,
      from: { opacity: 0 },
      to: { opacity: 1 },
      ease: a.motion.ease.outCubic,
      fill: 'forwards',
    })
    timeline.bind(state, {
      id: 'tracking-amplitude',
      label: 'Four bounded faults drive the colour and strip displacement',
      values: { fault: faultSignal },
    })

    layers.canvas2d({
      id: 'signal-image',
      render({ context2d: ctx, clock }) {
        const t = Math.max(0, Math.min(3.999999, clock.sourceSeconds))
        const slot = Math.floor(t * STEP_HZ + 0.000001)
        const fault = state.fault * intensity
        const burst = fault > 0.001
        const sampleSeed = seed + slot * 173

        ctx.fillStyle = BACKGROUND
        ctx.fillRect(0, 0, W, H)
        ctx.save()
        ctx.globalAlpha = 0.115
        a.surface.drawCanvas(ctx, grains[slot % 2], {
          x: -4 + Math.floor(noise(seed, slot + 301) * 8),
          y: -4 + Math.floor(noise(seed, slot + 911) * 8),
          width: W + 8,
          height: H + 8,
        })
        ctx.restore()

        // Partition, then shift entire pieces. No duplicate full-word slide or scene change.
        /** @type {{top:number,bottom:number,shift:number,separation:number}[]} */
        const tears = []
        if (burst) {
          const count = 4 + Math.floor(noise(sampleSeed, 21) * 5)
          for (let index = 0; index < count; index++) {
            const top = 194 + noise(sampleSeed, index * 9 + 5) * 492
            const height = 8 + noise(sampleSeed, index * 9 + 6) ** 2 * 64
            tears.push({
              top,
              bottom: Math.min(704, top + height),
              shift: signed(noise(sampleSeed, index * 9 + 7)) * (20 + fault * 132),
              separation: 1 + noise(sampleSeed, index * 9 + 8) * 0.7,
            })
          }
          tears.push({
            top: 746,
            bottom: 793,
            shift: signed(noise(sampleSeed, 97)) * fault * 15,
            separation: 0.58,
          })
        }
        const boundaries = [
          ...new Set([0, H, ...tears.flatMap((tear) => [tear.top, tear.bottom])]),
        ].sort((left, right) => left - right)
        const baseSeparation = 5.5 + fault * 19
        ctx.save()
        ctx.globalAlpha = clamp(state.opacity)
        for (let index = 0; index < boundaries.length - 1; index++) {
          const top = boundaries[index]
          const bottom = boundaries[index + 1]
          const mid = (top + bottom) / 2
          const tear = tears.find((candidate) => mid >= candidate.top && mid < candidate.bottom)
          const shift = tear?.shift ?? 0
          const separation = baseSeparation * (tear?.separation ?? 1)
          ctx.save()
          ctx.beginPath()
          ctx.rect(0, top, W, bottom - top)
          ctx.clip()
          a.surface.drawCanvas(ctx, plates[1], { x: shift - separation, y: 0, width: W, height: H })
          a.surface.drawCanvas(ctx, plates[2], { x: shift + separation, y: 0, width: W, height: H })
          a.surface.drawCanvas(ctx, plates[0], { x: shift, y: 0, width: W, height: H })
          ctx.restore()
        }
        ctx.restore()

        if (burst) {
          for (let index = 0; index < 7; index++) {
            const x = noise(sampleSeed, 150 + index) * (W - 100)
            const y = 225 + noise(sampleSeed, 250 + index) * 652
            ctx.globalAlpha = (index === 0 ? 0.7 : 0.36) * fault
            ctx.fillStyle = index % 3 === 0 ? accentLeft : index % 3 === 1 ? accentRight : '#d7e9e6'
            ctx.fillRect(x, y, 32 + noise(sampleSeed, 350 + index) * 225, 4 + (index % 3) * 5)
          }
          ctx.globalAlpha = 1
        }

        // A single 15 Hz sample of SMPTE-like receiver fallback, optional for quiet variants.
        if (showColorBars && intensity > 0.3 && (slot === 23 || slot === 52)) {
          ctx.globalAlpha = 0.86
          const colors = [
            '#d4d4bf',
            '#dfd923',
            '#20c7d4',
            '#1fd263',
            '#d424b2',
            '#d9473d',
            '#2b40cc',
          ]
          for (let index = 0; index < colors.length; index++) {
            ctx.fillStyle = colors[index]
            ctx.fillRect((index * W) / 7, 0, W / 7 + 1, H * 0.69)
          }
          ctx.fillStyle = '#0b0f17'
          ctx.fillRect(0, H * 0.69, W, H * 0.31)
          for (let index = 0; index < 5; index++) {
            ctx.fillStyle = ['#16284e', '#b6b9ab', '#362057', '#080a0c', '#272b29'][index]
            ctx.fillRect((index * W) / 5, H * 0.75, W / 5 + 1, H * 0.2)
          }
          ctx.globalAlpha = 1
        }

        if (showHud) {
          ctx.font = hudFont
          ctx.fillStyle = '#d6ebe6'
          ctx.textAlign = 'left'
          ctx.textBaseline = 'alphabetic'
          const tracking = burst
          ctx.fillRect(64, 65, 7, 28)
          ctx.fillRect(83, 65, 7, 28)
          ctx.fillText(tracking ? 'TRACKING' : 'PLAY', 120, 88)
          ctx.textAlign = 'right'
          ctx.fillText(`CH ${String(channel).padStart(2, '0')}`, 1856, 88)
          ctx.textAlign = 'left'
          const frames = 12 * STEP_HZ + 14 + slot
          const seconds = Math.floor(frames / STEP_HZ)
          const frame = frames % STEP_HZ
          ctx.fillText(
            `SP  0:${String(seconds).padStart(2, '0')}:${String(frame).padStart(2, '0')}`,
            64,
            1007,
          )
        }

        // Fine dark scanlines and a restrained edge vignette sit over both word and grain.
        ctx.fillStyle = 'rgba(0,8,10,0.16)'
        for (let y = 0; y < H; y += 4) ctx.fillRect(0, y + (slot % 2), W, 1.1)
        const vignette = ctx.createRadialGradient(W * 0.5, H * 0.46, 240, W * 0.5, H * 0.46, 1050)
        vignette.addColorStop(0, 'rgba(0,0,0,0)')
        vignette.addColorStop(1, 'rgba(0,0,0,0.22)')
        ctx.fillStyle = vignette
        ctx.fillRect(0, 0, W, H)
      },
    })
    return {
      dispose() {
        for (const plate of plates) plate.release()
        for (const grain of grains) grain.release()
      },
    }
  },
})
