package com.phoenix.bolotni.model

import kotlin.math.*

/**
 * Нативный препроцессинг аудио на Kotlin строго по параметрам model_config.json.
 * Полностью повторяет pipeline audio_utils.py (PyTorch / torchaudio):
 * Ресемплинг -> Обрезка/Паддинг -> Оконная функция Ханна -> STFT -> HTK Mel Filterbank -> Log-Mel -> Z-score -> 3 канала.
 */
class AudioPreprocessor(val config: ModelConfig = ModelConfig()) {

    private val hannWindow: FloatArray = FloatArray(config.nFft) { i ->
        (0.5 * (1.0 - cos(2.0 * PI * i / config.nFft))).toFloat()
    }

    private val melFilterBank: Array<FloatArray> = createMelFilterBank(
        nFft = config.nFft,
        nMels = config.nMels,
        sr = config.targetSr,
        fMin = 0.0f,
        fMax = config.targetSr / 2.0f
    )

    /**
     * Ресемплирование одномерного аудиосигнала до config.targetSr методом линейной интерполяции.
     */
    fun resampleIfNeeded(audio: FloatArray, sourceSr: Int): FloatArray {
        if (sourceSr == config.targetSr || audio.isEmpty()) return audio
        val ratio = config.targetSr.toDouble() / sourceSr.toDouble()
        val outLen = max(1, (audio.size * ratio).toInt())
        val resampled = FloatArray(outLen)
        for (i in 0 until outLen) {
            val srcIdx = i / ratio
            val idx0 = srcIdx.toInt()
            val idx1 = min(idx0 + 1, audio.size - 1)
            val frac = (srcIdx - idx0).toFloat()
            resampled[i] = audio[idx0] * (1.0f - frac) + audio[idx1] * frac
        }
        return resampled
    }

    /**
     * Обрезка по центру или zero-padding до targetLengthSamples (160 000 отсчётов для 5 секунд при 32 кГц).
     */
    fun cropOrPad(audio: FloatArray, targetLen: Int = config.targetLengthSamples): FloatArray {
        val len = audio.size
        if (len == targetLen) return audio
        val out = FloatArray(targetLen)
        if (len > targetLen) {
            val start = (len - targetLen) / 2
            System.arraycopy(audio, start, out, 0, targetLen)
        } else {
            System.arraycopy(audio, 0, out, 0, len)
        }
        return out
    }

    /**
     * Преобразование аудио в лог-мел спектрограмму формы [3, n_mels, num_frames].
     * Возвращает плоский массив FloatArray размера 3 * n_mels * num_frames.
     */
    fun processToLogMel(audio: FloatArray, timeShiftMs: Int = 0): FloatArray {
        var chunk = cropOrPad(audio, config.targetLengthSamples)

        // TTA time shift
        if (timeShiftMs != 0) {
            val shiftSamples = ((timeShiftMs / 1000.0) * config.targetSr).toInt()
            chunk = rollAudio(chunk, shiftSamples)
        }

        val nFft = config.nFft
        val hop = config.hop
        val nMels = config.nMels
        val nFreqs = nFft / 2 + 1 // 513

        // Center padding: nFft / 2 слева и справа
        val pad = nFft / 2
        val paddedAudio = FloatArray(chunk.size + 2 * pad)
        System.arraycopy(chunk, 0, paddedAudio, pad, chunk.size)

        val numFrames = (paddedAudio.size - nFft) / hop + 1

        // Вычисление STFT мощности: power = |STFT|^2
        val powerSpec = Array(numFrames) { FloatArray(nFreqs) }
        val realBuf = FloatArray(nFft)
        val imagBuf = FloatArray(nFft)

        for (frameIdx in 0 until numFrames) {
            val start = frameIdx * hop
            for (i in 0 until nFft) {
                realBuf[i] = paddedAudio[start + i] * hannWindow[i]
                imagBuf[i] = 0.0f
            }

            fftRadix2(realBuf, imagBuf, nFft)

            for (k in 0 until nFreqs) {
                val r = realBuf[k]
                val im = imagBuf[k]
                powerSpec[frameIdx][k] = r * r + im * im
            }
        }

        // Применение мел-фильтров: mel = spec.T @ mel_filterbank
        // mel[m, frame] = sum_k (powerSpec[frame][k] * melFilterBank[m][k])
        val logMel = Array(nMels) { FloatArray(numFrames) }
        var sum = 0.0
        var sumSq = 0.0
        val totalElements = nMels * numFrames

        for (m in 0 until nMels) {
            val filter = melFilterBank[m]
            for (f in 0 until numFrames) {
                var melVal = 0.0f
                val framePower = powerSpec[f]
                for (k in 0 until nFreqs) {
                    val w = filter[k]
                    if (w > 0.0f) {
                        melVal += framePower[k] * w
                    }
                }
                val logVal = ln(max(melVal, 1e-10f).toDouble()).toFloat()
                logMel[m][f] = logVal
                sum += logVal
                sumSq += logVal * logVal
            }
        }

        // Нормализация (logmel - mean) / (std + 1e-6)
        val mean = (sum / totalElements).toFloat()
        val variance = max(0.0, (sumSq / totalElements) - (mean.toDouble() * mean)).toFloat()
        val std = sqrt(variance)

        for (m in 0 until nMels) {
            for (f in 0 until numFrames) {
                logMel[m][f] = (logMel[m][f] - mean) / (std + 1e-6f)
            }
        }

        // Формирование 3 каналов [3, nMels, numFrames] в плоский буфер
        val flatOutput = FloatArray(3 * nMels * numFrames)
        val channelSize = nMels * numFrames
        var idx = 0
        for (c in 0 until 3) {
            for (m in 0 until nMels) {
                for (f in 0 until numFrames) {
                    flatOutput[idx++] = logMel[m][f]
                }
            }
        }

        return flatOutput
    }

    private fun rollAudio(audio: FloatArray, shift: Int): FloatArray {
        val len = audio.size
        if (len == 0 || shift == 0) return audio
        val effectiveShift = ((shift % len) + len) % len
        val out = FloatArray(len)
        System.arraycopy(audio, len - effectiveShift, out, 0, effectiveShift)
        System.arraycopy(audio, 0, out, effectiveShift, len - effectiveShift)
        return out
    }

    /**
     * Создание матрицы HTK Mel Filterbank: [nMels][nFreqs]
     */
    private fun createMelFilterBank(
        nFft: Int,
        nMels: Int,
        sr: Int,
        fMin: Float,
        fMax: Float
    ): Array<FloatArray> {
        val nFreqs = nFft / 2 + 1
        val filterBank = Array(nMels) { FloatArray(nFreqs) }

        fun hzToMel(hz: Float): Float = (2595.0 * log10(1.0 + hz / 700.0)).toFloat()
        fun melToHz(mel: Float): Float = (700.0 * (10.0.pow(mel / 2595.0) - 1.0)).toFloat()

        val minMel = hzToMel(fMin)
        val maxMel = hzToMel(fMax)

        val melPoints = FloatArray(nMels + 2)
        val hzPoints = FloatArray(nMels + 2)
        val binPoints = IntArray(nMels + 2)

        for (i in 0 until nMels + 2) {
            melPoints[i] = minMel + i * (maxMel - minMel) / (nMels + 1)
            hzPoints[i] = melToHz(melPoints[i])
            binPoints[i] = floor((nFft + 1) * hzPoints[i] / sr).toInt().coerceIn(0, nFreqs - 1)
        }

        for (m in 1..nMels) {
            val fPrev = binPoints[m - 1]
            val fCurr = binPoints[m]
            val fNext = binPoints[m + 1]

            // Восходящее плечо
            if (fCurr > fPrev) {
                for (k in fPrev..fCurr) {
                    filterBank[m - 1][k] = (k - fPrev).toFloat() / (fCurr - fPrev)
                }
            }
            // Нисходящее плечо
            if (fNext > fCurr) {
                for (k in fCurr..fNext) {
                    filterBank[m - 1][k] = (fNext - k).toFloat() / (fNext - fCurr)
                }
            }
        }

        return filterBank
    }

    /**
     * Быстрое преобразование Фурье (Radix-2 In-place Cooley-Tukey FFT).
     * Длина N должна быть степенью двойки (например 1024).
     */
    private fun fftRadix2(real: FloatArray, imag: FloatArray, n: Int) {
        var j = 0
        for (i in 0 until n - 1) {
            if (i < j) {
                val tempR = real[i]
                real[i] = real[j]
                real[j] = tempR
                val tempI = imag[i]
                imag[i] = imag[j]
                imag[j] = tempI
            }
            var k = n shr 1
            while (k <= j) {
                j -= k
                k = k shr 1
            }
            j += k
        }

        var len = 2
        while (len <= n) {
            val halfLen = len shr 1
            val angle = -2.0 * PI / len
            val wStepR = cos(angle).toFloat()
            val wStepI = sin(angle).toFloat()

            var i = 0
            while (i < n) {
                var wR = 1.0f
                var wI = 0.0f
                for (k in 0 until halfLen) {
                    val uR = real[i + k]
                    val uI = imag[i + k]
                    val vR = real[i + k + halfLen] * wR - imag[i + k + halfLen] * wI
                    val vI = real[i + k + halfLen] * wI + imag[i + k + halfLen] * wR

                    real[i + k] = uR + vR
                    imag[i + k] = uI + vI
                    real[i + k + halfLen] = uR - vR
                    imag[i + k + halfLen] = uI - vI

                    val nextWR = wR * wStepR - wI * wStepI
                    val nextWI = wR * wStepI + wI * wStepR
                    wR = nextWR
                    wI = nextWI
                }
                i += len
            }
            len = len shl 1
        }
    }
}
