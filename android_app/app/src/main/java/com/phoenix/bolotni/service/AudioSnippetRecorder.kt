package com.phoenix.bolotni.service

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Локальная запись коротких 5-секундных аудио-фрагментов подтверждённых детекций.
 * Сохраняет только подтверждённые окна (не пишет непрерывный поток), соблюдая приватность пользователя (Задача 3).
 */
class AudioSnippetRecorder(private val context: Context) {

    private val snippetsDir: File by lazy {
        val dir = File(context.cacheDir, "audio_snippets")
        if (!dir.exists()) dir.mkdirs()
        dir
    }

    /**
     * Запись FloatArray в формате 16-bit PCM WAV (32 кГц, моно).
     */
    fun saveWavSnippet(audio: FloatArray, sampleRate: Int = 32000, prefix: String = "detection"): File {
        val timestamp = System.currentTimeMillis()
        val file = File(snippetsDir, "${prefix}_$timestamp.wav")

        val numSamples = audio.size
        val byteRate = sampleRate * 2 // 16-bit mono = 2 bytes per sample
        val dataSize = numSamples * 2
        val totalSize = 36 + dataSize

        FileOutputStream(file).use { fos ->
            // RIFF header
            fos.write("RIFF".toByteArray())
            fos.write(intToByteArray(totalSize))
            fos.write("WAVE".toByteArray())

            // "fmt " subchunk
            fos.write("fmt ".toByteArray())
            fos.write(intToByteArray(16)) // subchunk1 size
            fos.write(shortToByteArray(1)) // AudioFormat (1 = PCM)
            fos.write(shortToByteArray(1)) // NumChannels (1 = Mono)
            fos.write(intToByteArray(sampleRate))
            fos.write(intToByteArray(byteRate))
            fos.write(shortToByteArray(2)) // BlockAlign
            fos.write(shortToByteArray(16)) // BitsPerSample

            // "data" subchunk
            fos.write("data".toByteArray())
            fos.write(intToByteArray(dataSize))

            // PCM audio samples
            val pcmBuf = ByteArray(dataSize)
            val byteBuf = ByteBuffer.wrap(pcmBuf).order(ByteOrder.LITTLE_ENDIAN)
            for (sample in audio) {
                val clamped = sample.coerceIn(-1.0f, 1.0f)
                val pcm16 = (clamped * 32767.0f).toInt().toShort()
                byteBuf.putShort(pcm16)
            }
            fos.write(pcmBuf)
        }

        enforceStorageLimit(maxSizeMb = 50)
        return file
    }

    /**
     * Очистка старых сниппетов при превышении лимита размера кэша (Задача 3 [P1]).
     */
    fun enforceStorageLimit(maxSizeMb: Long = 50) {
        val maxBytes = maxSizeMb * 1024 * 1024
        val files = snippetsDir.listFiles() ?: return
        var totalBytes = files.sumOf { it.length() }

        if (totalBytes > maxBytes) {
            val sorted = files.sortedBy { it.lastModified() }
            for (f in sorted) {
                totalBytes -= f.length()
                f.delete()
                if (totalBytes <= maxBytes) break
            }
        }
    }

    private fun intToByteArray(value: Int): ByteArray {
        return ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(value).array()
    }

    private fun shortToByteArray(value: Short): ByteArray {
        return ByteBuffer.allocate(2).order(ByteOrder.LITTLE_ENDIAN).putShort(value).array()
    }
}
