package com.phoenix.bolotni

import com.phoenix.bolotni.model.AudioPreprocessor
import com.phoenix.bolotni.model.ModelConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sin

class AudioPreprocessorTest {

    @Test
    fun testCropOrPad() {
        val preprocessor = AudioPreprocessor(ModelConfig())
        val targetLen = 160000

        // Тест с коротким массивом (паддинг)
        val shortAudio = FloatArray(1000) { 1.0f }
        val padded = preprocessor.cropOrPad(shortAudio, targetLen)
        assertEquals(targetLen, padded.size)
        assertEquals(1.0f, padded[0], 1e-5f)
        assertEquals(0.0f, padded[1000], 1e-5f)

        // Тест с длинным массивом (обрезка по центру)
        val longAudio = FloatArray(200000) { i -> i.toFloat() }
        val cropped = preprocessor.cropOrPad(longAudio, targetLen)
        assertEquals(targetLen, cropped.size)
        val expectedStart = (200000 - targetLen) / 2
        assertEquals(expectedStart.toFloat(), cropped[0], 1e-5f)
    }

    @Test
    fun testProcessToLogMelDimensions() {
        val config = ModelConfig(
            targetSr = 32000,
            nFft = 1024,
            hop = 320,
            nMels = 128,
            clipSeconds = 5.0f
        )
        val preprocessor = AudioPreprocessor(config)

        // Генерируем 5-секундный синусоидальный сигнал 440 Гц
        val numSamples = config.targetLengthSamples
        val synthAudio = FloatArray(numSamples) { i ->
            (sin(2.0 * Math.PI * 440.0 * i / config.targetSr) * 0.5).toFloat()
        }

        val features = preprocessor.processToLogMel(synthAudio)

        // Форма должна быть [3, 128, 501] -> 3 * 128 * 501 = 192384 элементов
        val expectedSize = 3 * config.nMels * config.numFrames
        assertEquals(expectedSize, features.size)

        // Проверяем конечность значений (без NaN и Inf)
        for (v in features) {
            assertTrue(!v.isNaN() && !v.isInfinite())
        }
    }
}
