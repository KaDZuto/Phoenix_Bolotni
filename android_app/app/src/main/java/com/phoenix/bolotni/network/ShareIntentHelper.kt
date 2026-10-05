package com.phoenix.bolotni.network

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.content.FileProvider
import com.phoenix.bolotni.local.DetectionEntity
import java.io.File

/**
 * Помощник отправки через системный Android Share-Intent (Задача 4 [P2] и Задача 7 [P0]).
 * Не требует прямой интеграции с Telegram API или интернета в момент отправки —
 * операционная система Android сама ставит задачу в очередь.
 */
object ShareIntentHelper {

    /**
     * Отправка аудио-фрагмента на экспертную разметку (в чат с ботом-разметчиком в Telegram).
     */
    fun shareSnippetForExpertReview(
        context: Context,
        detection: DetectionEntity
    ): Intent? {
        val snippetPath = detection.audioSnippetPath ?: return null
        val file = File(snippetPath)
        if (!file.exists()) return null

        val authority = "${context.packageName}.fileprovider"
        val contentUri: Uri = FileProvider.getUriForFile(context, authority, file)

        val caption = buildString {
            append("🎧 Запись для экспертной разметки (Феникс Болотный)\n")
            append("Вид (предварительно): ${detection.speciesRu} (${detection.speciesSlug})\n")
            append("Уверенность: %.1f%%\n".format(detection.confidence * 100))
            if (detection.lat != null && detection.lon != null) {
                append("Координаты: %.5f, %.5f\n".format(detection.lat, detection.lon))
            }
        }

        val shareIntent = Intent(Intent.ACTION_SEND).apply {
            type = "audio/wav"
            putExtra(Intent.EXTRA_STREAM, contentUri)
            putExtra(Intent.EXTRA_TEXT, caption)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        return Intent.createChooser(shareIntent, "Отправить на экспертную разметку в Telegram")
    }

    /**
     * Экспорт файла наблюдений (CSV или GeoJSON) через диалог «Поделиться».
     */
    fun shareExportedFile(context: Context, file: File, mimeType: String): Intent {
        val authority = "${context.packageName}.fileprovider"
        val contentUri: Uri = FileProvider.getUriForFile(context, authority, file)

        val shareIntent = Intent(Intent.ACTION_SEND).apply {
            type = mimeType
            putExtra(Intent.EXTRA_STREAM, contentUri)
            putExtra(Intent.EXTRA_SUBJECT, "Экспорт наблюдений: ${file.name}")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        return Intent.createChooser(shareIntent, "Сохранить или отправить экспорт")
    }
}
