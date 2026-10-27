"""Procedural music visualizer styles drawn onto a pygame surface.

Owns: the VisualizerDeck (style list, shuffle queue, per-channel style memory,
animation state) and the per-style drawing code.
Does not own: audio analysis or theming. The caller passes the amplitude and
the theme in on every render call.

WARNING: the render loop fills the surface with (1, 1, 1), never pure black.
(0, 0, 0) is the game-overlay transparency key.
"""
import pygame
import random
import colorsys
import math
import logging
import time as _time

try:
    import performance_governor as _governor
except Exception:
    _governor = None


# ============================================================================
# SECTION: Logger
# ============================================================================
_LOGGER = logging.getLogger(__name__)
_LOGGER.addHandler(logging.NullHandler())


# ============================================================================
# SECTION: Equalizer Grid helpers
# ============================================================================


def _eq_tunables():
    """Returns the Equalizer Grid numbers (smooth seconds, peak fall per second, longest step seconds) from the governor table."""
    # WARNING: the fallback numbers must match the EQ_GRID_* values in performance_governor.py; they are used only when that module fails to import.
    if _governor is None:
        return 0.12, 0.42, 0.1
    return (_governor.EQ_GRID_SMOOTH_SECONDS, _governor.EQ_GRID_PEAK_FALL_PER_SECOND,
            _governor.EQ_GRID_MAX_STEP_SECONDS)


def _eq_ease(current, target, dt, smooth_seconds):
    """Moves current toward target by the share of the gap that dt seconds cover, the same at any frame rate."""
    if smooth_seconds <= 0.0:
        return target
    return current + (target - current) * (1.0 - math.exp(-dt / smooth_seconds))


def _flight_tunables():
    """Returns the flying-style numbers (ease seconds, starfield slowest and fastest speed, tunnel slowest and fastest speed, longest step seconds) from the governor table."""
    # WARNING: the fallback numbers must match the FLIGHT_* values in performance_governor.py; they are used only when that module fails to import.
    if _governor is None:
        return 3.0, 0.35, 1.4, 0.40, 1.10, 0.1
    return (_governor.FLIGHT_SPEED_EASE_SECONDS, _governor.FLIGHT_STARFIELD_MIN_SPEED, _governor.FLIGHT_STARFIELD_MAX_SPEED,
            _governor.FLIGHT_TUNNEL_MIN_SPEED, _governor.FLIGHT_TUNNEL_MAX_SPEED, _governor.FLIGHT_MAX_STEP_SECONDS)


def _flight_speed(current, level, dt, ease_seconds, slowest, fastest):
    """Eases a flying speed toward the speed a music level (0 to 1) asks for, and never lets it fall below the slowest steady speed."""
    target = slowest + (fastest - slowest) * max(0.0, min(1.0, level))
    return max(slowest, _eq_ease(current, target, dt, ease_seconds))


# VALUES CHANGE HERE (real-music visualizers: seconds a level takes to rise toward a louder reading, and to fall toward a quieter one; beat glow fade seconds)
_REACT_ATTACK_SECONDS = 0.035
_REACT_RELEASE_SECONDS = 0.16
_REACT_BEAT_FADE_SECONDS = 0.18


def _react_at(react, p):
    """Returns the music level (0 to 1) at position p (0 = lowest pitch, 1 = highest): the 24-band spectrum in Performance, the 3 bands blended smoothly in Standard."""
    p = max(0.0, min(1.0, p))
    spectrum = react["spectrum"]
    if spectrum is not None:
        x = p * (len(spectrum) - 1)
        i = int(x)
        j = min(len(spectrum) - 1, i + 1)
        return spectrum[i] + (spectrum[j] - spectrum[i]) * (x - i)
    bands = react["bands"]
    x = p * 2.0
    i = min(1, int(x))
    return bands[i] + (bands[i + 1] - bands[i]) * (x - i)


def _eq_column(level, max_dots):
    """Splits a column level (in dots) into the whole dots to light and the fade, 0 to 1, of the next dot; always at least one dot, never past the top."""
    level = max(1.0, min(float(max_dots), level))
    whole = int(level)
    if whole >= max_dots:
        return max_dots, 0.0
    return whole, level - whole


# ============================================================================
# SECTION: Visualizer deck
# ============================================================================


class VisualizerDeck:
    # ========================================================================
    # SECTION: State & setup
    # ========================================================================
    def __init__(self):
        self.active_mode = "Random"
        self.current_running_style = "PrismShards"
        self.time_delta = 0.0
        self.smooth_amp = 50.0
        self._react_state = None

        self.render_styles = [
            "PrismShards", "LiquidChrome", "PulseRings",
            "WarpTunnel", "GlowBraid", "EqualizerGrid", "ParticleFlow", "NeonStarfield",
            "Lightning"
        ]

        self.shuffle_queue = []
        self.last_random_style = None
        self.style_by_channel = {}

        self.particles = [{"x": random.uniform(0, 1024), "y": random.uniform(0, 768), "size": random.uniform(1, 5), "speed": random.uniform(1, 4), "color": random.uniform(0, 1), "angle": random.uniform(0, math.pi*2), "angleSpeed": random.uniform(-0.02, 0.02)} for _ in range(200)]
        self.stars = [{"x": random.uniform(-1, 1), "y": random.uniform(-1, 1), "z": random.uniform(0.1, 1.0), "color": random.uniform(0, 1)} for _ in range(200)]

        self.eq_bars    = [0.0] * 24
        self.eq_targets = [random.uniform(0.05, 0.3) for _ in range(24)]
        self.eq_peaks   = [0.0] * 24
        self.eq_bar_phase = [random.uniform(0, math.pi * 2) for _ in range(24)]
        self.eq_bar_mult  = [random.uniform(0.75, 1.25) for _ in range(24)]
        self._eq_last_time = None

        _flight = _flight_tunables()
        self.star_speed = _flight[1]
        self.tunnel_speed = _flight[3]
        self.tunnel_depth = 0.0
        self._flight_last_time = None

        self.trail_surface = None
        self._glow_braid_fade = None
        self.lissa_phase = 0.0

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

        self.chrome_balls = []
        self._chrome_small = None
        for _ in range(7):
            self.chrome_balls.append({
                "x": random.uniform(100, 924), "y": random.uniform(100, 668),
                "vx": random.uniform(-1.6, 1.6), "vy": random.uniform(-1.6, 1.6),
                "base_r": random.uniform(55, 110), "hue": random.uniform(0, 1),
            })

        self.pulse_active = []
        self.pulse_spawn_timer = 0.0
        self.pulse_last_amp = 50.0

        self.lightning_bolts = []
        self.lightning_last_amp = 50.0
        self.lightning_flash = 0.0

    # ========================================================================
    # SECTION: Style selection
    # ========================================================================

    def get_next_random_style(self):
        if not self.shuffle_queue:
            self.shuffle_queue = self.render_styles.copy()
            random.shuffle(self.shuffle_queue)
            if self.last_random_style and self.shuffle_queue and self.shuffle_queue[0] == self.last_random_style:
                self.shuffle_queue.append(self.shuffle_queue.pop(0))
        next_style = self.shuffle_queue.pop(0)
        self.last_random_style = next_style
        return next_style

    # ========================================================================
    # SECTION: Lightning geometry helpers
    # ========================================================================

    def _jagged_path(self, p0, p1, displace, iterations):
        pts = [p0, p1]
        for _ in range(iterations):
            new_pts = []
            for i in range(len(pts) - 1):
                (x0, y0) = pts[i]
                (x1, y1) = pts[i + 1]
                mx = (x0 + x1) / 2.0 + random.uniform(-displace, displace)
                my = (y0 + y1) / 2.0 + random.uniform(-displace * 0.4, displace * 0.4)
                new_pts.append((x0, y0))
                new_pts.append((mx, my))
            new_pts.append(pts[-1])
            pts = new_pts
            displace *= 0.55
        return [(int(x), int(y)) for (x, y) in pts]

    def _build_lightning_bolt(self, w, h, start_x, hit):
        segments = []

        end_x = start_x + random.uniform(-w * 0.12, w * 0.12)
        ground_y = h * random.uniform(0.9, 1.0)
        main = self._jagged_path((start_x, -10), (end_x, ground_y), w * 0.08, 5)
        for i in range(len(main) - 1):
            segments.append((main[i], main[i + 1], 0))

        branch_budget = 2 + int(hit * 4)
        for _ in range(branch_budget):
            bi = random.randint(1, max(1, len(main) - 2))
            bx, by = main[bi]
            length = random.uniform(h * 0.12, h * 0.28)
            ang = math.radians(random.uniform(35, 80))
            ex = bx + math.cos(ang) * length * random.choice([-1, 1])
            ey = by + math.sin(ang) * length
            branch = self._jagged_path((bx, by), (ex, ey), w * 0.04, 3)
            for i in range(len(branch) - 1):
                segments.append((branch[i], branch[i + 1], 1))

            if random.random() < 0.4 and len(branch) > 2:
                si = random.randint(1, len(branch) - 2)
                sx, sy = branch[si]
                sub_end = (sx + random.uniform(-w * 0.05, w * 0.05), sy + length * 0.5)
                sub = self._jagged_path((sx, sy), sub_end, w * 0.025, 2)
                for i in range(len(sub) - 1):
                    segments.append((sub[i], sub[i + 1], 2))

        return {
            "segments": segments,
            "life": 1.0,
            "hue": random.uniform(0.55, 0.72),
            "width": 2 + int(hit * 3),
        }

    # ========================================================================
    # SECTION: Render entry points
    # ========================================================================

    def _prepare_reactive(self, reactive, dt):
        """Smooths the real-music numbers (quick rise, slower fall) and returns the dict the styles read, or None to keep the Lite fake motion."""
        if not reactive:
            self._react_state = None
            return None
        state = self._react_state
        if state is None:
            state = {"level": 0.0, "bands": [0.0, 0.0, 0.0], "spectrum": None, "pulse": 0.0}
            self._react_state = state

        def follow(current, target):
            return _eq_ease(current, target, dt,
                            _REACT_ATTACK_SECONDS if target > current else _REACT_RELEASE_SECONDS)

        state["level"] = follow(state["level"], reactive["level"])
        state["bands"] = [follow(c, t) for c, t in zip(state["bands"], reactive["bands"])]
        incoming = reactive.get("spectrum")
        if incoming is None:
            state["spectrum"] = None
        else:
            held = state["spectrum"]
            if held is None or len(held) != len(incoming):
                held = [0.0] * len(incoming)
            state["spectrum"] = [follow(c, t) for c, t in zip(held, incoming)]
        state["pulse"] = max(reactive["beat"], state["pulse"] * math.exp(-dt / _REACT_BEAT_FADE_SECONDS))

        bands = state["bands"]
        centroid = (bands[1] * 0.5 + bands[2]) / (sum(bands) + 1e-6)
        spectrum = state["spectrum"]
        peak_pos = (spectrum.index(max(spectrum)) / float(len(spectrum) - 1)) if spectrum else centroid
        return {
            "level": state["level"], "bands": bands, "spectrum": spectrum,
            "beat": reactive["beat"], "pulse": state["pulse"],
            "centroid": centroid, "peak_pos": peak_pos, "full": spectrum is not None,
        }

    def render_active_engine(self, surface, theme, mock_amplitude=None, style_override=None, reactive=None):
        try:
            return self._render_active_engine(
                surface, theme, mock_amplitude, style_override, reactive
            )
        except Exception as error:
            _LOGGER.error("Visualizer render failed for style %r: %s", style_override, error)
            raise

    def _render_active_engine(self, surface, theme, mock_amplitude=None, style_override=None, reactive=None):
        w, h = surface.get_size()
        self.time_delta += 0.04
        _flight_ease, _star_min, _star_max, _tunnel_min, _tunnel_max, _flight_max_step = _flight_tunables()
        _flight_now = _time.monotonic()
        flight_dt = (1.0 / 60.0) if self._flight_last_time is None else min(_flight_max_step, max(0.0, _flight_now - self._flight_last_time))
        self._flight_last_time = _flight_now

        react = self._prepare_reactive(reactive, flight_dt)
        if react is None:
            target_amp = mock_amplitude if mock_amplitude is not None else 50
            self.smooth_amp += (target_amp - self.smooth_amp) * 0.15
        else:
            self.smooth_amp = 8.0 + react["level"] * 92.0
        amp = self.smooth_amp

        if style_override is not None:
            style = style_override
        else:
            style = self.current_running_style if self.active_mode == "Random" else self.active_mode

        # Pulsate is the shared beat-driven expansion/glow layer. Keep normal
        # level and spectrum reactivity for styles that do not use that effect.
        react_pulse = 0.0 if style in {"GlowBraid", "Kaleidoscope", "Lightning"} else (react["pulse"] if react is not None else 0.0)

        # WARNING: (1, 1, 1), not pure black: (0, 0, 0) is the game-overlay transparency key.
        surface.fill((1, 1, 1))
        t = self.time_delta
        amp_norm = amp / 256.0

        cx, cy = w // 2, h // 2
        max_r = math.hypot(cx, cy) * 1.1

        def _xf(x, y, base):
            cb, sb = math.cos(base), math.sin(base)
            return (int(cx + x * cb - y * sb), int(cy + x * sb + y * cb))


        if style == "PrismShards":
            slices = self.prism_slices
            half = self.prism_half
            slice_angle = half * 2.0
            global_rot = t * 0.15

            for shard in self.prism_shards:
                local_angle = shard["a_frac"] * half
                if react is None:
                    radius = shard["r_frac"] * max_r * (1.0 + amp_norm * 0.12)
                else:
                    radius = shard["r_frac"] * max_r * (1.0 + _react_at(react, shard["r_frac"]) * 0.30 + react_pulse * 0.12)
                cx0 = radius * math.cos(local_angle)
                cy0 = radius * math.sin(local_angle)
                breathe = 1.0 + 0.18 * math.sin(t * 1.4 + shard["phase"])
                if react is not None:
                    breathe += _react_at(react, shard["a_frac"]) * 0.25 + react_pulse * 0.10
                spin = shard["spin"] * t
                csp, ssp = math.cos(spin), math.sin(spin)

                hue = (shard["hue"] + t * 0.05) % 1.0
                shard_value = 0.95 if react is None else min(1.0, 0.45 + _react_at(react, shard["r_frac"]) * 0.55 + react_pulse * 0.2)
                cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.85, shard_value)
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

            if react is None:
                radii = [b["base_r"] * (0.8 + amp_norm * 0.9) for b in self.chrome_balls]
            else:
                ball_count = max(1, len(self.chrome_balls) - 1)
                radii = [b["base_r"] * (0.75 + _react_at(react, i / float(ball_count)) * 0.9 + react_pulse * 0.25)
                         for i, b in enumerate(self.chrome_balls)]

            small = self._chrome_small
            if small is None or small.get_size() != (cols, rows):
                small = pygame.Surface((cols, rows))
                self._chrome_small = small
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


        elif style == "PulseRings":
            self.pulse_spawn_timer += 1
            if react is None:
                spawn_interval = max(5, int(16 - amp_norm * 12))
                onset = (amp - self.pulse_last_amp) > 8
                spawn_now = self.pulse_spawn_timer >= spawn_interval or onset
            else:
                spawn_now = react["beat"] > 0.0 or self.pulse_spawn_timer >= 90
            self.pulse_last_amp = amp

            if spawn_now:
                self.pulse_spawn_timer = 0
                if react is None:
                    self.pulse_active.append({
                        "r": 0.0,
                        "hue": (t * 0.1) % 1.0,
                        "speed": 4.0 + amp_norm * 9.0,
                    })
                else:
                    self.pulse_active.append({
                        "r": 0.0,
                        "hue": (0.95 - 0.6 * react["centroid"]) % 1.0,
                        "speed": 4.0 + react["level"] * 9.0 + react["beat"] * 6.0,
                        "shape": ([_react_at(react, abs(k / 24.0 - 1.0)) for k in range(48)] if react["full"] else None),
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
                shape = ring.get("shape")
                if shape is not None and ring["r"] >= 4:
                    ring_pts = [(int(cx + math.cos(k * math.pi / 24.0) * ring["r"] * (1.0 + 0.18 * shape[k])),
                                 int(cy + math.sin(k * math.pi / 24.0) * ring["r"] * (1.0 + 0.18 * shape[k])))
                                for k in range(48)]
                    pygame.draw.lines(surface, color, True, ring_pts, thickness)
                else:
                    pygame.draw.circle(surface, color, (cx, cy), int(ring["r"]), thickness)

            core_r = 14 + amp_norm * 34 if react is None else 14 + react["level"] * 40 + react_pulse * 20
            cr, cg, cb = colorsys.hsv_to_rgb((t * 0.15) % 1.0, 0.55, 1.0)
            pygame.draw.circle(surface, (int(cr * 255), int(cg * 255), int(cb * 255)), (cx, cy), int(core_r))


        elif style == "WarpTunnel":
            sides = 6
            num_rings = 16
            self.tunnel_speed = _flight_speed(self.tunnel_speed, amp_norm if react is None else react["level"], flight_dt, _flight_ease, _tunnel_min, _tunnel_max)
            self.tunnel_depth = (self.tunnel_depth + self.tunnel_speed * flight_dt) % 1.0

            base_unit = min(w, h) * 0.045
            for i in range(num_rings):
                depth = ((i / float(num_rings)) - self.tunnel_depth) % 1.0
                scale = 1.0 / (depth + 0.06)
                radius = scale * base_unit
                if react is not None:
                    radius *= 1.0 + react_pulse * 0.08
                if radius > max(w, h) * 1.3:
                    continue

                rot = t * (0.3 + depth * 0.5) + i * 0.4
                hue = (depth + t * 0.05) % 1.0
                bright = max(0.15, 1.0 - depth)
                cr, cg, cb = colorsys.hsv_to_rgb(hue, 0.9, bright)
                color = (int(cr * 255), int(cg * 255), int(cb * 255))
                if react is None:
                    thickness = max(1, int(3 * bright + amp_norm * 5))
                else:
                    thickness = max(1, int(3 * bright + _react_at(react, i / float(num_rings - 1)) * 8 + react_pulse * 4))

                pts = []
                for s in range(sides):
                    ang = rot + s * (math.pi * 2.0 / sides)
                    pts.append((int(cx + math.cos(ang) * radius), int(cy + math.sin(ang) * radius)))
                if len(pts) >= 3:
                    pygame.draw.polygon(surface, color, pts, thickness)

            core_r = 8 + amp_norm * 16 if react is None else 8 + react["level"] * 20 + react_pulse * 10
            cr, cg, cb = colorsys.hsv_to_rgb((t * 0.05) % 1.0, 0.5, 1.0)
            pygame.draw.circle(surface, (int(cr * 255), int(cg * 255), int(cb * 255)), (cx, cy), int(core_r))


        elif style == "GlowBraid":
            if self.trail_surface is None or self.trail_surface.get_size() != (w, h):
                self.trail_surface = pygame.Surface((w, h), pygame.SRCALPHA)
                self.trail_surface.fill((0, 0, 0, 0))

            if self._glow_braid_fade is None or self._glow_braid_fade.get_size() != (w, h):
                self._glow_braid_fade = pygame.Surface((w, h), pygame.SRCALPHA)
                self._glow_braid_fade.fill((0, 0, 0, 16))
            self.trail_surface.blit(self._glow_braid_fade, (0, 0), special_flags=pygame.BLEND_RGBA_SUB)

            self.lissa_phase += 0.015 + (amp_norm if react is None else react["level"] * 0.4) * 0.05
            cx2, cy2 = w / 2.0, h / 2.0
            braid_amp = 0.75 + amp_norm * 0.5

            freq_a = 3.0 + math.sin(t * 0.07) * 1.4 + (amp_norm * 0.8 if react is None else react["centroid"] * 1.2)
            freq_b = 2.0 + math.cos(t * 0.05) * 1.1

            strands = 6 if (react is not None and react["full"]) else 3
            samples = 220
            for s_i in range(strands):
                if react is not None:
                    braid_amp = 0.70 + _react_at(react, s_i / float(max(1, strands - 1))) * 0.55 + react_pulse * 0.10
                rx = w * 0.48 * braid_amp
                ry = h * 0.48 * braid_amp
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
                    pygame.draw.lines(self.trail_surface, glow_color, True, pts, 5)
                    pygame.draw.lines(self.trail_surface, bright_color, True, pts, 2)

            surface.blit(self.trail_surface, (0, 0))


        elif style == "EqualizerGrid":
            t = self.time_delta
            num_bars = 24
            bar_w = w // num_bars
            dot_h = max(4, bar_w - 4)
            dot_w = max(4, bar_w - 6)
            gap = 3
            cell_h = dot_h + gap
            _EQ_TOP_MARGIN = 10
            max_dots = (h - _EQ_TOP_MARGIN) // cell_h

            _eq_smooth, _eq_peak_fall, _eq_max_step = _eq_tunables()
            if react is not None:
                _eq_smooth = min(_eq_smooth, 0.05)
            _eq_now = _time.monotonic()
            _eq_dt = (1.0 / 60.0) if self._eq_last_time is None else min(_eq_max_step, max(0.0, _eq_now - self._eq_last_time))
            self._eq_last_time = _eq_now

            for i in range(num_bars):
                center_boost = 1.0 - abs(i - num_bars/2.0) / (num_bars/2.0) * 0.35
                wobble = 0.85 + 0.15 * math.sin(t * 1.6 + self.eq_bar_phase[i])
                if react is None:
                    self.eq_targets[i] = min(1.0, (amp / 120.0) * center_boost * self.eq_bar_mult[i] * wobble)
                else:
                    bar_level = _react_at(react, i / float(num_bars - 1))
                    if not react["full"]:
                        bar_level *= 0.9 + 0.2 * (self.eq_bar_mult[i] - 0.75) / 0.5
                    self.eq_targets[i] = min(1.0, bar_level * 1.05)
                self.eq_bars[i] = _eq_ease(self.eq_bars[i], self.eq_targets[i], _eq_dt, _eq_smooth)
                if self.eq_bars[i] >= self.eq_peaks[i]:
                    self.eq_peaks[i] = self.eq_bars[i]
                else:
                    self.eq_peaks[i] = max(0.0, self.eq_peaks[i] - _eq_peak_fall * _eq_dt)

            _EQ_BOTTOM = (255, 60, 170)
            _EQ_MID    = (0, 210, 220)
            _EQ_TOP    = (210, 230, 60)

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
                num_filled, top_fade = _eq_column(self.eq_bars[bi] * max_dots, max_dots)

                for di in range(num_filled):
                    dot_frac = di / float(max(1, max_dots))
                    dot_color = _eq_row_color(dot_frac)
                    dot_y = h - dot_h - di * cell_h
                    if dot_y > 0:
                        pygame.draw.rect(surface, dot_color, (bar_x, dot_y, dot_w, dot_h))

                if top_fade >= 0.04:
                    fade_y = h - dot_h - num_filled * cell_h
                    if fade_y > 0:
                        fade_color = _eq_row_color(num_filled / float(max(1, max_dots)))
                        fade_color = tuple(int(1 + (c - 1) * top_fade) for c in fade_color)
                        pygame.draw.rect(surface, fade_color, (bar_x, fade_y, dot_w, dot_h))

                peak_di = int(self.eq_peaks[bi] * max_dots)
                peak_y = h - dot_h - peak_di * cell_h
                if peak_y > 0 and peak_di > 2:
                    pygame.draw.rect(surface, (255, 240, 80), (bar_x, peak_y, dot_w, dot_h))

        elif style == "ParticleFlow":
            for p in self.particles:
                if react is None:
                    p["y"] -= p["speed"] * (1.0 + amp * 0.01)
                else:
                    p["y"] -= p["speed"] * (1.0 + _react_at(react, p["color"]) * 1.2 + react_pulse * 1.0)
                p["angle"] += p["angleSpeed"]
                p["x"] += math.sin(p["angle"]) * 3
                if p["y"] < 0:
                    p["y"] = h
                    p["x"] = random.uniform(0, w)
                if react is None:
                    scale = (amp / 80.0) + 0.6
                else:
                    scale = 0.6 + _react_at(react, p["color"]) * 1.6 + react_pulse * 0.5
                r, g, b = colorsys.hsv_to_rgb(p["color"], 1.0, 1.0)
                pygame.draw.circle(surface, (int(r*255), int(g*255), int(b*255)), (int(p["x"]), int(p["y"])), max(1, int(p["size"] * scale)))

        elif style == "NeonStarfield":
            self.star_speed = _flight_speed(self.star_speed, amp_norm if react is None else react["level"], flight_dt, _flight_ease, _star_min, _star_max)
            for s in self.stars:
                s["z"] -= self.star_speed * flight_dt
                if s["z"] <= 0:
                    s["z"] = 1.0
                    s["x"] = random.uniform(-1, 1)
                    s["y"] = random.uniform(-1, 1)
                sx = int((s["x"] / s["z"]) * (w / 2) + (w / 2))
                sy = int((s["y"] / s["z"]) * (h / 2) + (h / 2))
                if react is None:
                    r_sz = max(1, int((1.0 - s["z"]) * (4 + amp * 0.05)))
                    star_value = 1.0
                else:
                    star_level = _react_at(react, s["color"])
                    r_sz = max(1, int((1.0 - s["z"]) * (3 + star_level * 5 + react_pulse * 4)))
                    star_value = min(1.0, 0.45 + star_level * 0.55 + react_pulse * 0.2)
                r, g, b = colorsys.hsv_to_rgb(s["color"], 1.0, star_value)
                if 0 <= sx < w and 0 <= sy < h:
                    pygame.draw.circle(surface, (int(r*255), int(g*255), int(b*255)), (sx, sy), r_sz)


        elif style == "Lightning":
            onset_delta = amp - self.lightning_last_amp
            self.lightning_last_amp = amp

            if react is None:
                beat = onset_delta > 7.2
            else:
                beat = react["beat"] > 0.0

            if beat:

                hit = max(0.0, min(1.0, onset_delta / 7.5)) if react is None else react["beat"]
                num_bolts = 1 + int(hit * 2)
                for _ in range(num_bolts):
                    if react is not None and react["full"]:
                        start_x = max(w * 0.1, min(w * 0.9, w * (0.1 + 0.8 * react["peak_pos"]) + random.uniform(-w * 0.05, w * 0.05)))
                    else:
                        start_x = random.uniform(w * 0.15, w * 0.85)
                    self.lightning_bolts.append(
                        self._build_lightning_bolt(w, h, start_x, hit))

                self.lightning_flash = min(1.0, self.lightning_flash + hit * 0.6 + 0.2)


            decay = 0.06 + amp_norm * 0.10
            alive = []
            for bolt in self.lightning_bolts:
                bolt["life"] -= decay
                if bolt["life"] > 0.0:
                    alive.append(bolt)
            self.lightning_bolts = alive

            self.lightning_flash = max(0.0, self.lightning_flash - 0.08)
            if self.lightning_flash > 0.01:
                fv = int(28 * self.lightning_flash)
                surface.fill((fv, fv, min(255, fv + 12)))

            for bolt in self.lightning_bolts:
                glow = max(0.0, min(1.0, bolt["life"]))
                gr, gg, gb = colorsys.hsv_to_rgb(bolt["hue"], 0.75, 0.55 * glow)
                glow_c = (int(gr * 255), int(gg * 255), int(gb * 255))
                base_w = bolt["width"]
                for (p1, p2, depth) in bolt["segments"]:
                    gw = max(1, (base_w - depth) + 3)
                    pygame.draw.line(surface, glow_c, p1, p2, gw)

            for bolt in self.lightning_bolts:
                glow = max(0.0, min(1.0, bolt["life"]))
                cr, cg, cb = colorsys.hsv_to_rgb(bolt["hue"], 0.20, 0.6 + 0.4 * glow)
                core_c = (int(cr * 255), int(cg * 255), int(cb * 255))
                base_w = bolt["width"]
                for (p1, p2, depth) in bolt["segments"]:
                    cw = max(1, base_w - depth)
                    pygame.draw.line(surface, core_c, p1, p2, cw)


        else:
            _LOGGER.error("Unknown visualizer style: %r", style)
            font = pygame.font.SysFont(None, 28)
            msg = font.render(f"[visualizer] unrecognized style: {style!r}", True, (255, 60, 60))
            surface.blit(msg, (20, 20))


# ============================================================================
# SECTION: Exports
# ============================================================================
__all__ = ["VisualizerDeck"]
