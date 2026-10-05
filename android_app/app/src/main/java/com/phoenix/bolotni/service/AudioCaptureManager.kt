package com.phoenix.bolotni.service

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.phoenix.bolotni.model.AudioPreprocessor
import com.phoenix.bolotni.model.ModelConfig
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Непрерывный захват звука с микрофона в кольцевой буфер без пауз и разрывов (Задача 2 [P0]).
 * Нарезает поток на перекрывающиеся 5-секундные окна с шагом window_stride_sec.
 */
class AudioCaptureManager(
    val config: ModelConfig,
    private val preprocessor: AudioPreprocessor,
    private val onWindowReady: (windowIndex: Long, audioWindow: FloatArray) -> Unit
) {
    private val isRecording = AtomicBoolean(false)
    private var recordingJob: Job? = null

    private var audioRecord: AudioRecord? = null
    private var actualSampleRate: Int = config.targetSr

    // Длина окна и шага в отсчётах
    private val windowSamples: Int = config.targetLengthSamples // 160 000
    private val strideSamples: Int = config.strideSamples // 80 000

    @SuppressLint("MissingPermission")
    fun start(scope: CoroutineScope) {
        if (isRecording.getAndSet(true)) return

        recordingJob = scope.launch(Dispatchers.IO) {
            setupAudioRecord()
            val record = audioRecord ?: run {
                isRecording.set(false)
                return@launch
            }

            try {
                record.startRecording()
            } catch (e: Exception) {
                isRecording.set(false)
                return@launch
            }

            // Кольцевой буфер для непрерывного накопления
            val ringBufferSize = windowSamples * 3
            val ringBuffer = FloatArray(ringBufferSize)
            var writeHead = 0
            var totalSamplesRead = 0L
            var nextWindowThreshold = windowSamples.toLong()
            var windowIndex = 0L

            val readChunkSize = 2048
            val pcmBuffer = ShortArray(readChunkSize)

            while (isActive && isRecording.get()) {
                val readCount = record.read(pcmBuffer, 0, readChunkSize)
                if (readCount > 0) {
                    // Конвертация 16-bit PCM в нормализованный Float [-1.0, 1.0]
                    var floatChunk = FloatArray(readCount) { i -> pcmBuffer[i] / 32768.0f }

                    // Ресемплинг при несовпадении частоты микрофона с целевой
                    if (actualSampleRate != config.targetSr) {
                        floatChunk = preprocessor.resampleIfNeeded(floatChunk, actualSampleRate)
                    }

                    for (sample in floatChunk) {
                        ringBuffer[writeHead] = sample
                        writeHead = (writeHead + 1) % ringBufferSize
                        totalSamplesRead++

                        if (totalSamplesRead >= nextWindowThreshold) {
                            // Формируем окно длиной windowSamples
                            val window = FloatArray(windowSamples)
                            val startPos = (writeHead - windowSamples + ringBufferSize) % ringBufferSize
                            for (w in 0 until windowSamples) {
                                window[w] = ringBuffer[(startPos + w) % ringBufferSize]
                            }

                            onWindowReady(windowIndex++, window)
                            nextWindowThreshold += strideSamples
                        }
                    }
                } else if (readCount < 0) {
                    delay(20)
                }
            }

            try {
                record.stop()
                record.release()
            } catch (ignored: Exception) {}
            audioRecord = null
        }
    }

    fun stop() {
        isRecording.set(false)
        recordingJob?.cancel()
        recordingJob = null
    }

    @SuppressLint("MissingPermission")
    private fun setupAudioRecord() {
        // Пробуем сначала 32000 Гц, затем 44100 / 48000 если устройство не поддерживает 32кГц напрямую
        val sampleRatesToTry = intArrayOf(config.targetSr, 48000, 44100, 16000)
        for (sr in sampleRatesToTry) {
            val minBufSize = AudioRecord.getMinBufferSize(
                sr,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            if (minBufSize > 0) {
                try {
                    val record = AudioRecord(
                        MediaRecorder.AudioSource.MIC,
                        sr,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT,
                        minBufSize * 2
                    )
                    if (record.state == AudioRecord.STATE_INITIALIZED) {
                        audioRecord = record
                        actualSampleRate = sr
                        return
                    }
                } catch (ignored: Exception) {}
            }
        }
    }
}
