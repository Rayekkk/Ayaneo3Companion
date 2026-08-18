-- AYANEO 3 OLED, EDID AYA / 0x0113 / AYAOLED_FHD
--
-- The EDID contains sRGB-like primaries even though AYANEO specifies the
-- physical panel as 110% DCI-P3. Use Display-P3/D65 as the closest documented
-- description of the panel until unit-specific measurements are available.
local colorimetry = {
    r = { x = 0.6800, y = 0.3200 },
    g = { x = 0.2650, y = 0.6900 },
    b = { x = 0.1500, y = 0.0600 },
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
