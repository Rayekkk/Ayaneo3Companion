-- AYANEO 3 OLED, EDID AYA / 0x0113 / AYAOLED_FHD
--
-- Nominal RGB primaries from the DXQ7D0023 panel specification. Use D65 as the
-- output white: the module's nominal 0.3000/0.3100 white is customer-adjustable
-- and causes an incorrect warm cast in gamescope's PQ-to-Gamma-2.2 transform on
-- this AYANEO unit. The bridge-provided sRGB-like primaries are also incorrect.
local colorimetry = {
    r = { x = 0.6820, y = 0.3150 },
    g = { x = 0.2400, y = 0.7160 },
    b = { x = 0.1380, y = 0.0460 },
    w = { x = 0.3127, y = 0.3290 },
}

local rates = { 60, 90, 120, 144 }

gamescope.config.known_displays.ayaneo_ayaneo3_oled = {
    pretty_name = "AYANEO 3 OLED",
    colorimetry = colorimetry,
    dynamic_refresh_rates = rates,
    hdr = {
        supported = true,
        eotf = gamescope.eotf.gamma22,
        -- AYANEO specifies 800 nits as the complete device's maximum global
        -- and manual brightness. The bare module's nominal 1000-nit HBM mode
        -- is not verified as active in this Gamma-2.2 output path.
        max_content_light_level = 800,
        max_frame_average_luminance = 400,
        min_content_light_level = 0.007,
    },
    dynamic_modegen = function(base_mode, refresh)
        local mode = base_mode
        gamescope.modegen.set_resolution(mode, 1080, 1920)
        gamescope.modegen.set_h_timings(mode, 80, 44, 156)
        gamescope.modegen.set_v_timings(mode, 48, 2, 14)
        mode.clock = gamescope.modegen.calc_max_clock(mode, refresh)
        mode.vrefresh = gamescope.modegen.calc_vrefresh(mode)
        return mode
    end,
    matches = function(display)
        if display.vendor == "AYA" and (display.product == 0x0113 or display.model == "AYAOLED_FHD") then
            return 5000
        end
        return -1
    end,
}
debug("Registered AYANEO 3 OLED as a known display")
