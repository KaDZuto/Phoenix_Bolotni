package com.phoenix.bolotni

import com.phoenix.bolotni.model.ModelConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ModelConfigTest {

    @Test
    fun testDefaultConfig() {
        val config = ModelConfig()
        assertEquals(32000, config.targetSr)
        assertEquals(1024, config.nFft)
        assertEquals(320, config.hop)
        assertEquals(128, config.nMels)
        assertEquals(5.0f, config.clipSeconds, 1e-4f)
        assertEquals("_noise", config.noiseClassSlug)
        assertEquals(0.6f, config.confidenceThreshold, 1e-4f)
        assertEquals(2.5f, config.windowStrideSec, 1e-4f)
        assertEquals(3, config.windowCountForVote)
        assertEquals(2, config.minAgreeWindows)
        assertEquals(160000, config.targetLengthSamples)
        assertEquals(80000, config.strideSamples)
        assertEquals(501, config.numFrames)
    }

    @Test
    fun testFromJsonParsing() {
        val json = """
            {
                "target_sr": 32000,
                "n_fft": 1024,
                "hop": 320,
                "n_mels": 128,
                "clip_seconds": 5.0,
                "noise_class_slug": "_noise",
                "confidence_threshold": 0.7,
                "window_stride_sec": 2.5,
                "window_count_for_vote": 4,
                "min_agree_windows": 3,
                "tta_time_shift_ms": [-100, 100]
            }
        """.trimIndent()

        val config = ModelConfig.fromJson(json)
        assertEquals(0.7f, config.confidenceThreshold, 1e-4f)
        assertEquals(4, config.windowCountForVote)
        assertEquals(3, config.minAgreeWindows)
        assertEquals(listOf(-100, 100), config.ttaTimeShiftMs)
    }
}
