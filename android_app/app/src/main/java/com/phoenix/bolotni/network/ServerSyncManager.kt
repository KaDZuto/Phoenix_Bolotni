package com.phoenix.bolotni.network

import android.content.Context
import com.phoenix.bolotni.local.DetectionDao
import com.phoenix.bolotni.local.DetectionEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

data class SyncResult(
    val success: Boolean,
    val syncedCount: Int,
    val errorMessage: String? = null
)

/**
 * Менеджер синхронизации статистики с сервером (Задача 4 [P2]).
 * Опционален, по умолчанию выключен.
 * Передаёт только статистические метаданные детекций (без сырого аудио).
 */
class ServerSyncManager(
    private val context: Context,
    private val dao: DetectionDao
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    /**
     * Попытка синхронизации накопленных офлайн-детекций с сервером.
     */
    suspend fun syncPendingDetections(serverBaseUrl: String, deviceId: String): SyncResult = withContext(Dispatchers.IO) {
        val pending = dao.getPendingSync(limit = 100)
        if (pending.isEmpty()) {
            return@withContext SyncResult(success = true, syncedCount = 0)
        }

        val jsonArray = JSONArray()
        for (item in pending) {
            val obj = JSONObject().apply {
                put("species_slug", item.speciesSlug)
                put("confidence", item.confidence)
                put("window_start_ts", item.windowStartTs)
                put("window_end_ts", item.windowEndTs)
                put("lat", item.lat ?: JSONObject.NULL)
                put("lon", item.lon ?: JSONObject.NULL)
                put("created_at", item.createdAt)
                put("device_id", deviceId)
            }
            jsonArray.put(obj)
        }

        val ingestUrl = if (serverBaseUrl.endsWith("/")) "${serverBaseUrl}ingest" else "$serverBaseUrl/ingest"

        val body = jsonArray.toString().toRequestBody(jsonMediaType)
        val request = Request.Builder()
            .url(ingestUrl)
            .post(body)
            .build()

        try {
            val response = client.newCall(request).execute()
            response.use { resp ->
                if (resp.isSuccessful) {
                    val syncedIds = pending.map { it.id }
                    dao.markAsSynced(syncedIds)
                    SyncResult(success = true, syncedCount = syncedIds.size)
                } else {
                    SyncResult(success = false, syncedCount = 0, errorMessage = "HTTP ${resp.code}: ${resp.message}")
                }
            }
        } catch (e: Exception) {
            SyncResult(success = false, syncedCount = 0, errorMessage = e.message)
        }
    }
}
