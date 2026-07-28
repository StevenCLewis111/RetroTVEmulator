# ==============================================================================
# PART 1 OF 4: CORE ENGINE INIT ARRAYS & DYNAMIC ASSET POOLS
# ==============================================================================

import pygame
import random
import colorsys
import math

class VisualizerDeck:
    def __init__(self):
        print("[TELEMETRY - VIS PART 1] Instantiating audio engine visualizer matrix registers.")
        self.active_mode = "Random"
        self.current_running_style = "PrismShards"
        self.time_delta = 0.0
        self.smooth_amp = 50.0

        self.modes_list = [
            "Random", "PrismShards", "LiquidChrome", "PulseRings",
            "WarpTunnel", "GlowBraid", "EqualizerGrid", "ParticleFlow", "NeonStarfield"
        ]

        self.render_styles = [
            "PrismShards", "LiquidChrome", "PulseRings",
            "WarpTunnel", "GlowBraid", "EqualizerGrid", "ParticleFlow", "NeonStarfield"
        ]

        self.shuffle_queue = []
        self.last_random_style = None
        # BUG FIX: current_running_style is a single global value shared by
        # every caller (the live tuned channel AND the TV guide's mini
        # preview of a DIFFERENT channel). That's why two Random-mode
        # visualizer channels viewed at the same time (e.g. main screen +
        # guide preview) always showed the identical style -- there was
        # nowhere to remember "channel 19 rolled X" separately from
        # "channel 20 rolled Y". style_by_channel gives each channel (keyed
        # by its zero-padded channel string) its own remembered style, only
        # updated when THAT channel's own scheduled track actually changes.
        self.style_by_channel = {}

        # ParticleFlow state (unchanged)
        self.particles = [{"x": random.uniform(0, 1024), "y": random.uniform(0, 768), "size": random.uniform(1, 5), "speed": random.uniform(1, 4), "color": random.uniform(0, 1), "angle": random.uniform(0, math.pi*2), "angleSpeed": random.uniform(-0.02, 0.02)} for _ in range(200)]
        # NeonStarfield state (unchanged)
        self.stars = [{"x": random.uniform(-1, 1), "y": random.uniform(-1, 1), "z": random.uniform(0.1, 1.0), "color": random.uniform(0, 1)} for _ in range(200)]

        # EqualizerGrid: per-bar smooth heights and peak hold
        self.eq_bars    = [0.0] * 24
        self.eq_targets = [random.uniform(0.05, 0.3) for _ in range(24)]
        self.eq_peaks   = [0.0] * 24
        # Fixed per-bar "personality" -- assigned ONCE here, not re-rolled
        # every frame. eq_bar_phase offsets each bar's wobble in time so
        # bars don't move in perfect lockstep; eq_bar_mult gives each bar a
        # consistent, stable reactivity (some bars a bit taller/shorter than
        # others every time the music hits) instead of a brand-new random
        # personality every time a target happens to re-roll.
        self.eq_bar_phase = [random.uniform(0, math.pi * 2) for _ in range(24)]
        self.eq_bar_mult  = [random.uniform(0.75, 1.25) for _ in range(24)]

        # GlowBraid: persistent phosphor-trail surface (faded each frame, not
        # cleared) plus the running phase for the woven Lissajous strands
        self.trail_surface = None
        self.lissa_phase = 0.0

        # PrismShards: persistent scattered crystal shards (irregular jagged
        # polygons at random size/position). The mirror-fold only looks like
        # a kaleidoscope if there's irregular, asymmetric detail for it to
        # reflect — a single smooth ring outline just mirrors into a flower.
        self.prism_slices = 8
        self.prism_half = math.pi / self.prism_slices
        self.prism_shards = []
        for _ in range(22):
            a_frac = random.uniform(0.05, 0.95)
            r_frac = random.uniform(0.10, 1.0)
            size = random.uniform(0.05, 0.16) * (0.5 + r_frac * 0.5)
            verts = random.randint(3, 5)
            local_pts = []
            for k in range(verts):
                va = (k / verts) * math.pi * 2 + random.uniform(-0.3, 0.3)
                vr = size * random.uniform(0.55, 1.15)
                local_pts.append((math.cos(va) * vr, math.sin(va) * vr))
            self.prism_shards.append({
                "a_frac": a_frac, "r_frac": r_frac, "verts": local_pts,
                "hue": random.uniform(0, 1), "phase": random.uniform(0, math.pi * 2),
                "spin": random.uniform(-0.4, 0.4),
            })

        # LiquidChrome: persistent blob pool (metaball field). Each blob has
        # its own position/velocity in a fixed baseline space and bounces
        # around; render-time clamping rescales that motion to whatever the
        # actual surface size is, same pattern used by ParticleFlow/Starfield.
        self.chrome_balls = []
        for _ in range(7):
            self.chrome_balls.append({
                "x": random.uniform(100, 924), "y": random.uniform(100, 668),
                "vx": random.uniform(-1.6, 1.6), "vy": random.uniform(-1.6, 1.6),
                "base_r": random.uniform(55, 110), "hue": random.uniform(0, 1),
            })

        # PulseRings: active expanding-ring pool + onset-detection state
        self.pulse_active = []
        self.pulse_spawn_timer = 0.0
        self.pulse_last_amp = 50.0

    def get_next_random_style(self):
        if not self.shuffle_queue:
            self.shuffle_queue = self.render_styles.copy()
            random.shuffle(self.shuffle_queue)
            if self.last_random_style and self.shuffle_queue and self.shuffle_queue[0] == self.last_random_style:
                self.shuffle_queue.append(self.shuffle_queue.pop(0))
        next_style = self.shuffle_queue.pop(0)
        self.last_random_style = next_style
        print(f"[VISUALIZER RANDOM] Selected style: {next_style} (remaining in queue: {len(self.shuffle_queue)})")
        return next_style

# ==============================================================================
# PART 2 OF 4: KALEIDOSCOPE ENGINES (STYLES 1-2)
# ==============================================================================
# Both engines below use real kaleidoscope math: a single angular "wedge" of
# geometry is mirrored across its own center line (so the two halves are
# true reflections of each other, not independently-drawn shapes) and then
# that mirrored wedge is rotated around the center N times with NO per-ring
# rotation drift, so every wedge boundary lines up seamlessly with its
# neighbor. That seamless mirror+repeat is what actually reads as a
# kaleidoscope rather than just "spinny shapes."

    def render_active_engine(self, surface, theme, mock_amplitude=None, style_override=None):
        w, h = surface.get_size()
        self.time_delta += 0.04

        target_amp = mock_amplitude if mock_amplitude is not None else 50
        self.smooth_amp += (target_amp - self.smooth_amp) * 0.15
        amp = self.smooth_amp

        # style_override lets a caller (e.g. the TV Guide mini-preview) render
        # a specific style for THIS call only, without touching
        # self.current_running_style. That field is the live channel's own
        # state; writing to it from anywhere else corrupts the live
        # visualizer for every other caller sharing this same
        # VisualizerDeck instance.
        if style_override is not None:
            style = style_override
        else:
            style = self.current_running_style if self.active_mode == "Random" else self.active_mode

        surface.fill((1, 1, 1))  # not pure black: (0,0,0) is the game-overlay transparency key
        t = self.time_delta
        amp_norm = amp / 256.0

        # Center point for all mirroring calculation reflections
        cx, cy = w // 2, h // 2
        max_r = math.hypot(cx, cy) * 1.1

        # Shared helper: takes a point in "local wedge space" — x = forward
        # distance, y = perpendicular distance from the wedge's own mirror
        # axis — rotates it by the wedge's world angle (base) and translates
        # to screen center. Passing y vs -y is literally the mirror fold.
        def _xf(x, y, base):
            cb, sb = math.cos(base), math.sin(base)
            return (int(cx + x * cb - y * sb), int(cy + x * sb + y * cb))

        # ── 1. PRISM SHARDS (Crystalline mirrored facets) ──
        # A scattered field of irregular jagged crystal shards lives in one
        # half-wedge. Each shard is mirrored across the wedge's own axis
        # (real reflection, not a redraw) and the whole wedge is repeated
        # around the circle with one shared rotation — true kaleidoscope
        # fold-and-repeat, with enough irregular detail in the source shapes
        # to actually look fractured instead of a smooth spinning flower.
        if style == "PrismShards":
            slices = self.prism_slices
            half = self.prism_half
            slice_angle = half * 2.0
            global_rot = t * 0.15

            for shard in self.prism_shards:
                local_angle = shard["a_frac"] * half
                radius = shard["r_frac"] * max_r * (1.0 + amp_norm * 0.12)
                cx0 = radius * math.cos(local_angle)
                cy0 = radius * math.sin(local_angle)
                breathe = 1.0 + 0.18 * math.sin(t * 1.4 + shard["phase"])
                spin = shard["spin"] * t
                csp, ssp = math.cos(spin), math.sin(spin)

                hue = (shard["hue"] + t * 0.05) % 1.0
                cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
                color = (int(cr * 255), int(cg * 255), int(cb * 255))
                edge_color = (min(255, color[0] + 60), min(255, color[1] + 60), min(255, color[2] + 60))

                local_pts = []
                for dx, dy in shard["verts"]:
                    dx2, dy2 = dx * max_r * breathe, dy * max_r * breathe
                    rx = dx2 * csp - dy2 * ssp
                    ry = dx2 * ssp + dy2 * csp
                    local_pts.append((cx0 + rx, cy0 + ry))

                for s in range(slices):
                    base = s * slice_angle + global_rot
                    for mirror in (1, -1):
                        poly = [_xf(x, mirror * y, base) for (x, y) in local_pts]
                        pygame.draw.polygon(surface, color, poly, 0)
                        pygame.draw.polygon(surface, edge_color, poly, 1)

        # ── 2. LIQUID CHROME (Merging metaball blobs) ──
        # A handful of soft circular "charges" drift and bounce around the
        # screen. At each point in a low-res sample grid we sum each blob's
        # influence (inverse-square falloff); past a threshold that field
        # reads as solid color, just below it as a soft glowing rim — which
        # is what makes nearby blobs visually melt into one another instead
        # of just overlapping as flat circles. The low-res grid (then
        # smooth-scaled up to full size) keeps this cheap enough to run
        # every frame in pure Python.
        elif style == "LiquidChrome":
            cols = 108
            rows = max(1, int(cols * h / float(w)))
            cell_w = w / float(cols)
            cell_h = h / float(rows)

            for b in self.chrome_balls:
                b["x"] += b["vx"] * (1.0 + amp_norm * 1.5)
                b["y"] += b["vy"] * (1.0 + amp_norm * 1.5)
                if b["x"] < 0:
                    b["x"] = 0; b["vx"] *= -1
                elif b["x"] > w:
                    b["x"] = w; b["vx"] *= -1
                if b["y"] < 0:
                    b["y"] = 0; b["vy"] *= -1
                elif b["y"] > h:
                    b["y"] = h; b["vy"] *= -1

            radii = [b["base_r"] * (0.8 + amp_norm * 0.9) for b in self.chrome_balls]

            small = pygame.Surface((cols, rows))
            for gy in range(rows):
                py = (gy + 0.5) * cell_h
                for gx in range(cols):
                    px = (gx + 0.5) * cell_w
                    field = 0.0
                    hue_acc = 0.0
                    for b, r in zip(self.chrome_balls, radii):
                        dx = px - b["x"]
                        dy = py - b["y"]
                        d2 = dx * dx + dy * dy + 1.0
                        contrib = (r * r) / d2
                        field += contrib
                        hue_acc += b["hue"] * contrib

                    if field > 0.9:
                        hue = (hue_acc / field + t * 0.04) % 1.0
                        bright = min(1.0, 0.55 + (field - 0.9) * 0.5)
                        cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.92, bright)
                        small.set_at((gx, gy), (int(cr * 255), int(cg * 255), int(cb * 255)))
                    elif field > 0.55:
                        edge = (field - 0.55) / 0.35
                        hue = (hue_acc / field + t * 0.04) % 1.0 if field > 0 else 0.0
                        cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.95, edge)
                        small.set_at((gx, gy), (int(cr * 255), int(cg * 255), int(cb * 255)))
                    else:
                        small.set_at((gx, gy), (1, 1, 1))

            scaled = pygame.transform.smoothscale(small, (w, h))
            surface.blit(scaled, (0, 0))

# ==============================================================================
# PART 3a OF 4: WAVEFORM ENGINES (STYLE 3 - RIBBON WAVE)
# ==============================================================================

        # ── 3. RIBBON WAVE (Smooth, Gentle Rainbow Mode) ──
        # ── 3. PULSE RINGS (Expanding audio-reactive shockwaves) ──
        # Rings spawn at the center and expand outward — a steady heartbeat
        # rate that speeds up with volume, plus an extra ring fired on any
        # sudden jump in amplitude (an "onset"), so loud hits visibly punch
        # out a new wave instead of just nudging an existing line sideways.
        elif style == "PulseRings":
            self.pulse_spawn_timer += 1
            spawn_interval = max(5, int(16 - amp_norm * 12))
            onset = (amp - self.pulse_last_amp) > 8
            self.pulse_last_amp = amp

            if self.pulse_spawn_timer >= spawn_interval or onset:
                self.pulse_spawn_timer = 0
                self.pulse_active.append({
                    "r": 0.0,
                    "hue": (t * 0.1) % 1.0,
                    "speed": 4.0 + amp_norm * 9.0,
                })

            edge_r = math.hypot(cx, cy) * 1.05
            still_alive = []
            for ring in self.pulse_active:
                ring["r"] += ring["speed"]
                if ring["r"] < edge_r:
                    still_alive.append(ring)
            self.pulse_active = still_alive

            for ring in self.pulse_active:
                frac = ring["r"] / edge_r
                fade = max(0.0, 1.0 - frac)
                thickness = max(2, int(10 * (1.0 - frac * 0.6)))
                cr, cg, cb = colorsys.hsv_to_rgb(ring["hue"], 1.0, 0.45 + 0.55 * fade)
                color = (int(cr * 255), int(cg * 255), int(cb * 255))
                pygame.draw.circle(surface, color, (cx, cy), int(ring["r"]), thickness)

            core_r = 14 + amp_norm * 34
            cr, cg, cb = colorsys.hsv_to_rgb((t * 0.15) % 1.0, 0.55, 1.0)
            pygame.draw.circle(surface, (int(cr * 255), int(cg * 255), int(cb * 255)), (cx, cy), int(core_r))

        # ── 4. WARP TUNNEL (Perspective ring tunnel rushing toward camera) ──
        # Hexagonal rings are spawned far away and scaled up with simple
        # 1/depth perspective as they approach, giving real forward motion
        # and depth instead of a flat envelope sitting on one plane. Audio
        # drives both how fast rings approach and how thick their edges are.
        elif style == "WarpTunnel":
            sides = 6
            num_rings = 16
            speed = 0.16 + amp_norm * 0.4

# ==============================================================================
# PART 3b OF 4: WAVEFORM ENGINES (WARP TUNNEL & GLOW BRAID)
# ==============================================================================

            base_unit = min(w, h) * 0.045
            for i in range(num_rings):
                depth = ((i / float(num_rings)) - t * speed) % 1.0
                scale = 1.0 / (depth + 0.06)
                radius = scale * base_unit
                if radius > max(w, h) * 1.3:
                    continue

                rot = t * (0.3 + depth * 0.5) + i * 0.4
                hue = (depth + t * 0.05) % 1.0
                bright = max(0.15, 1.0 - depth)
                cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.9, bright)
                color = (int(cr * 255), int(cg * 255), int(cb * 255))
                thickness = max(1, int(3 * bright + amp_norm * 5))

                pts = []
                for s in range(sides):
                    ang = rot + s * (math.pi * 2.0 / sides)
                    pts.append((int(cx + math.cos(ang) * radius), int(cy + math.sin(ang) * radius)))
                if len(pts) >= 3:
                    pygame.draw.polygon(surface, color, pts, thickness)

            # Bright core glow at the vanishing point
            core_r = 8 + amp_norm * 16
            cr, cg, cb = colorsys.hsv_to_rgb((t * 0.05) % 1.0, 0.5, 1.0)
            pygame.draw.circle(surface, (int(cr * 255), int(cg * 255), int(cb * 255)), (cx, cy), int(core_r))

        # ── 5. GLOW BRAID (Woven oscilloscope Lissajous trace) ──
        # A handful of glowing strands trace interlocking Lissajous curves
        # around the screen center, left burned onto a persistent surface
        # that only fades a little each frame — true phosphor-trail
        # persistence, not a redraw-and-clear. The strands' frequency ratio
        # and radius breathe with the audio, and their phases are offset so
        # they cross and braid around each other instead of overlapping as
        # one line. Nothing else in this file uses a decaying trail or a
        # parametric curve, so this reads as a completely different kind of
        # motion from the radial kaleidoscopes, blob fields, bar grids, and
        # particle/star fields above and below it.
        elif style == "GlowBraid":
            if self.trail_surface is None or self.trail_surface.get_size() != (w, h):
                self.trail_surface = pygame.Surface((w, h), pygame.SRCALPHA)
                self.trail_surface.fill((0, 0, 0, 0))

            # Phosphor decay: subtract a little alpha from the existing trail
            # every frame instead of clearing it, so strokes linger and
            # overlap into glowing braided knots before fading out.
            fade = pygame.Surface((w, h), pygame.SRCALPHA)
            fade.fill((0, 0, 0, 16))
            self.trail_surface.blit(fade, (0, 0), special_flags=pygame.BLEND_RGBA_SUB)

            self.lissa_phase += 0.015 + amp_norm * 0.05
            cx2, cy2 = w / 2.0, h / 2.0
            rx = w * 0.48 * (0.75 + amp_norm * 0.5)
            ry = h * 0.48 * (0.75 + amp_norm * 0.5)

            # Frequency ratio drifts slowly on its own so the braid pattern
            # never quite repeats, then gets an audio-driven kick on top.
            freq_a = 3.0 + math.sin(t * 0.07) * 1.4 + amp_norm * 0.8
            freq_b = 2.0 + math.cos(t * 0.05) * 1.1

            strands = 3
            samples = 220
            for s_i in range(strands):
                strand_phase = self.lissa_phase + s_i * (math.pi * 2.0 / strands) * 0.22
                hue = (t * 0.05 + s_i / float(strands)) % 1.0
                cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
                bright_color = (int(cr * 255), int(cg * 255), int(cb * 255), 255)
                glow_color = (int(cr * 255), int(cg * 255), int(cb * 255), 70)

                pts = []
                for p_i in range(samples):
                    frac = p_i / float(samples)
                    ang = frac * math.pi * 2.0
                    x = cx2 + math.sin(freq_a * ang + strand_phase) * rx
                    y = cy2 + math.sin(freq_b * ang + strand_phase * 1.3) * ry
                    pts.append((x, y))

                if len(pts) >= 2:
                    # Soft wide pass underneath for glow, crisp thin pass on
                    # top for a bright phosphor core.
                    pygame.draw.lines(self.trail_surface, glow_color, True, pts, 5)
                    pygame.draw.lines(self.trail_surface, bright_color, True, pts, 2)

            surface.blit(self.trail_surface, (0, 0))

# ==============================================================================
# PART 3c OF 4: SPECTRUM ENGINES (STYLE 6 - EQUALIZER GRID)
# ==============================================================================

        # ── 6. EQUALIZER GRID ──
        # Classic LED dot-matrix equalizer bars rising from the bottom.
        # Square/rect dots (NOT circles). Pink at bottom, teal/blue mid, yellow-green top.
        elif style == "EqualizerGrid":
            t = self.time_delta  # Defined local time reference variable to prevent unbound crash
            num_bars = 24
            bar_w = w // num_bars
            dot_h = max(4, bar_w - 4)   # square dots: height matches width
            dot_w = max(4, bar_w - 6)
            gap = 3
            cell_h = dot_h + gap
            max_dots = (h - 20) // cell_h

            # Animate bar targets every frame, directly off the current
            # amplitude -- previously each bar only got a new target on a
            # random 10% chance per frame, with the target itself partly
            # randomized, which is what made bars look like they were
            # vanishing/appearing at random instead of moving together with
            # the music. Now every bar tracks amp every frame; eq_bar_mult/
            # eq_bar_phase (fixed once at startup, see __init__) give each
            # bar its own stable height and a smooth, continuously-advancing
            # wobble -- some bars consistently run taller, some shorter, and
            # they gently offset from each other in time -- without any bar
            # ever freezing or jumping to a disconnected random value.
            for i in range(num_bars):
                center_boost = 1.0 - abs(i - num_bars/2.0) / (num_bars/2.0) * 0.35
                wobble = 0.85 + 0.15 * math.sin(t * 1.6 + self.eq_bar_phase[i])
                self.eq_targets[i] = min(1.0, (amp / 120.0) * center_boost * self.eq_bar_mult[i] * wobble)
                self.eq_bars[i] += (self.eq_targets[i] - self.eq_bars[i]) * 0.18
                if self.eq_bars[i] >= self.eq_peaks[i]:
                    self.eq_peaks[i] = self.eq_bars[i]
                else:
                    self.eq_peaks[i] = max(0.0, self.eq_peaks[i] - 0.007)

            # Fixed bottom-to-top color bands: pink at the bottom, teal/blue
            # through the middle, yellow-green at the top. Color depends
            # ONLY on a dot's row height (dot_frac) -- previously the hue
            # also subtracted a continuously-increasing time term, which
            # scrolled the whole gradient over time regardless of the music
            # and made colors look chaotic instead of stable bands that
            # simply get revealed further as a column stacks higher.
            _EQ_BOTTOM = (255, 60, 170)   # pink
            _EQ_MID    = (0, 210, 220)    # teal/blue
            _EQ_TOP    = (210, 230, 60)   # yellow-green

            def _eq_row_color(frac):
                frac = max(0.0, min(1.0, frac))
                if frac < 0.5:
                    lo, hi, f = _EQ_BOTTOM, _EQ_MID, frac / 0.5
                else:
                    lo, hi, f = _EQ_MID, _EQ_TOP, (frac - 0.5) / 0.5
                return (
                    int(lo[0] + (hi[0] - lo[0]) * f),
                    int(lo[1] + (hi[1] - lo[1]) * f),
                    int(lo[2] + (hi[2] - lo[2]) * f),
                )

            for bi in range(num_bars):
                bar_x = bi * bar_w + (bar_w - dot_w) // 2
                num_filled = max(1, int(self.eq_bars[bi] * max_dots))

                for di in range(num_filled):
                    dot_frac = di / float(max(1, max_dots))
                    dot_color = _eq_row_color(dot_frac)
                    dot_y = h - 10 - (di + 1) * cell_h
                    if dot_y > 0:
                        pygame.draw.rect(surface, dot_color, (bar_x, dot_y, dot_w, dot_h))

                # Peak hold dot — bright yellow-white cap
                peak_di = int(self.eq_peaks[bi] * max_dots)
                peak_y = h - 10 - (peak_di + 1) * cell_h
                if peak_y > 0 and peak_di > 2:
                    pygame.draw.rect(surface, (255, 240, 80), (bar_x, peak_y, dot_w, dot_h))

# ==============================================================================
# PART 4 OF 4: PRESERVED ENGINES - PARTICLE FLOW & NEON STARFIELD (UNCHANGED)
# ==============================================================================

        # 7. Particle Flow (UNCHANGED)
        elif style == "ParticleFlow":
            for idx, p in enumerate(self.particles):
                p["y"] -= p["speed"] * (1.0 + amp * 0.01)
                p["angle"] += p["angleSpeed"]
                p["x"] += math.sin(p["angle"]) * 3
                if p["y"] < 0:
                    p["y"] = h
                    p["x"] = random.uniform(0, w)
                scale = (amp / 80.0) + 0.6
                r, g, b = colorsys.hsv_to_rgb(p["color"], 1.0, 1.0)
                pygame.draw.circle(surface, (int(r*255), int(g*255), int(b*255)), (int(p["x"]), int(p["y"])), max(1, int(p["size"] * scale)))

        # 8. Neon Starfield Warp (UNCHANGED)
        elif style == "NeonStarfield":
            speed = (amp / 256.0) * 0.08 + 0.01
            for s in self.stars:
                s["z"] -= speed
                if s["z"] <= 0:
                    s["z"] = 1.0
                    s["x"] = random.uniform(-1, 1)
                    s["y"] = random.uniform(-1, 1)
                sx = int((s["x"] / s["z"]) * (w / 2) + (w / 2))
                sy = int((s["y"] / s["z"]) * (h / 2) + (h / 2))
                r_sz = max(1, int((1.0 - s["z"]) * (4 + amp * 0.05)))
                r, g, b = colorsys.hsv_to_rgb(s["color"], 1.0, 1.0)
                if 0 <= sx < w and 0 <= sy < h:
                    pygame.draw.circle(surface, (int(r*255), int(g*255), int(b*255)), (sx, sy), r_sz)

        # 9. Safety net — should never trigger if active_mode/style names stay in
        # sync with modes_list, but draws something visible instead of an
        # invisible black screen if a style name ever fails to match above.
        else:
            font = pygame.font.SysFont(None, 28)
            msg = font.render(f"[visualizer] unrecognized style: {style!r}", True, (255, 60, 60))
            surface.blit(msg, (20, 20))