package com.phoenix.bolotni.model

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.zip.ZipInputStream

data class ModelBundleManifest(
    val version: Int,
    val releaseDate: String,
    val bundleUrl: String,
    val sha256: String,
    val notes: String? = null
)

sealed class UpdateResult {
    data class Success(val version: Int) : UpdateResult()
    data object UpToDate : UpdateResult()
    data class Error(val message: String) : UpdateResult()
}

/**
 * Менеджер обновления модели без публикации в Google Play (Задача 6 [P2]).
 * Загружает бандл, проверяет контрольную сумму SHA-256, распаковывает и подменяет модель с возможностью отката.
 */
class ModelUpdateManager(
    private val context: Context,
    private val modelRunner: ModelRunner
) {
    private val client = OkHttpClient()

    private val modelsDir: File by lazy {
        val dir = File(context.filesDir, "updated_models")
        if (!dir.exists()) dir.mkdirs()
        dir
    }

    private val prefs = context.getSharedPreferences("model_version_prefs", Context.MODE_PRIVATE)

    fun getCurrentModelVersion(): Int {
        return prefs.getInt("current_version", 1)
    }

    /**
     * Проверка и загрузка обновления (не блокирует офлайн-работу приложения).
     */
    suspend fun checkAndUpdateModel(manifestUrl: String): UpdateResult = withContext(Dispatchers.IO) {
        try {
            val manifest = fetchManifest(manifestUrl) ?: return@withContext UpdateResult.Error("Не удалось получить манифест")
            val currentVer = getCurrentModelVersion()

            if (manifest.version <= currentVer) {
                return@withContext UpdateResult.UpToDate
            }

            // Скачиваем ZIP-бандл во временный файл
            val tempZip = File(modelsDir, "bundle_temp.zip")
            val downloadOk = downloadFile(manifest.bundleUrl, tempZip)
            if (!downloadOk) {
                return@withContext UpdateResult.Error("Ошибка скачивания бандла")
            }

            // Валидация контрольной суммы SHA-256
            val actualHash = calculateSha256(tempZip)
            if (!actualHash.equals(manifest.sha256, ignoreCase = true)) {
                tempZip.delete()
                return@withContext UpdateResult.Error("Несовпадение контрольной суммы SHA-256: ожидалось ${manifest.sha256}, получено $actualHash")
            }

            // Распаковываем в версию-специфичную директорию
            val targetDir = File(modelsDir, "v${manifest.version}")
            if (targetDir.exists()) targetDir.deleteRecursively()
            targetDir.mkdirs()

            val unzipOk = unzip(tempZip, targetDir)
            tempZip.delete()
            if (!unzipOk) {
                targetDir.deleteRecursively()
                return@withContext UpdateResult.Error("Ошибка распаковки ZIP-архива бандла")
            }

            val modelFile = File(targetDir, "bird_model.onnx")
            val classesFile = File(targetDir, "classes.json")
            val whitelistFile = File(targetDir, "ru_birds_whitelist.json")
            val configFile = File(targetDir, "model_config.json")

            if (!modelFile.exists() || !classesFile.exists()) {
                targetDir.deleteRecursively()
                return@withContext UpdateResult.Error("Бандл повреждён: отсутствуют ключевые файлы")
            }

            // Пробуем инициализировать модель
            try {
                modelRunner.initializeFromFiles(
                    modelFile = modelFile,
                    classesFile = classesFile,
                    whitelistFile = whitelistFile,
                    configFile = configFile
                )
            } catch (e: Exception) {
                // Откат к предыдущей стабильной версии или ассетам
                targetDir.deleteRecursively()
                rollbackToAssets()
                return@withContext UpdateResult.Error("Ошибка загрузки новой модели: ${e.message}. Произведён откат.")
            }

            // Успешно обновлено
            prefs.edit().putInt("current_version", manifest.version).apply()
            UpdateResult.Success(manifest.version)
        } catch (e: Exception) {
            UpdateResult.Error("Исключение при обновлении: ${e.message}")
        }
    }

    private fun rollbackToAssets() {
        try {
            modelRunner.initializeFromAssets(context)
            prefs.edit().putInt("current_version", 1).apply()
        } catch (ignored: Exception) {}
    }

    private fun fetchManifest(manifestUrl: String): ModelBundleManifest? {
        val req = Request.Builder().url(manifestUrl).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val body = resp.body?.string() ?: return null
            val obj = JSONObject(body)
            return ModelBundleManifest(
                version = obj.getInt("version"),
                releaseDate = obj.optString("release_date", ""),
                bundleUrl = obj.getString("bundle_url"),
                sha256 = obj.getString("sha256"),
                notes = obj.optString("notes", null)
            )
        }
    }

    private fun downloadFile(url: String, dest: File): Boolean {
        val req = Request.Builder().url(url).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return false
            resp.body?.byteStream()?.use { input ->
                FileOutputStream(dest).use { output ->
                    input.copyTo(output)
                }
            } ?: return false
        }
        return true
    }

    private fun calculateSha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(8192)
            var read: Int
            while (input.read(buf).also { read = it } != -1) {
                md.update(buf, 0, read)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun unzip(zipFile: File, destDir: File): Boolean {
        ZipInputStream(zipFile.inputStream()).use { zis ->
            var entry = zis.nextEntry
            while (entry != null) {
                val newFile = File(destDir, entry.name)
                if (entry.isDirectory) {
                    newFile.mkdirs()
                } else {
                    newFile.parentFile?.mkdirs()
                    FileOutputStream(newFile).use { fos ->
                        zis.copyTo(fos)
                    }
                }
                zis.closeEntry()
                entry = zis.nextEntry
            }
        }
        return true
    }
}
