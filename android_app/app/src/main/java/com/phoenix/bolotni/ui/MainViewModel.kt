package com.phoenix.bolotni.ui

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.phoenix.bolotni.local.*
import com.phoenix.bolotni.model.*
import com.phoenix.bolotni.network.ServerSyncManager
import com.phoenix.bolotni.network.SyncResult
import com.phoenix.bolotni.service.ContinuousListeningService
import com.phoenix.bolotni.service.ServiceListeningState
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File

class MainViewModel(application: Application) : AndroidViewModel(application) {

    private val db = DetectionsDatabase.getInstance(application)
    private val dao = db.detectionDao()
    private val statsAggregator = StatsAggregator(dao)
    private val syncManager = ServerSyncManager(application, dao)
    private val modelRunner = ModelRunner()
    private val updateManager = ModelUpdateManager(application, modelRunner)

    // Состояние сервиса прослушивания
    val listeningState: StateFlow<ServiceListeningState> = ContinuousListeningService.listeningState

    // Поток подтверждённых детекций из локальной БД
    val detections: StateFlow<List<DetectionEntity>> = dao.getRecentFlow(limit = 100)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    // Список всех доступных видов из ru_birds_whitelist.json
    private val _speciesList = MutableStateFlow<List<SpeciesInfo>>(emptyList())
    val speciesList: StateFlow<List<SpeciesInfo>> = _speciesList.asStateFlow()

    // Сводная статистика
    private val _statsSummary = MutableStateFlow<StatsSummary?>(null)
    val statsSummary: StateFlow<StatsSummary?> = _statsSummary.asStateFlow()

    // Настройки
    private val prefs = application.getSharedPreferences("phoenix_prefs", Context.MODE_PRIVATE)

    var isNetworkSyncEnabled: Boolean
        get() = prefs.getBoolean("network_sync_enabled", false)
        set(value) = prefs.edit().putBoolean("network_sync_enabled", value).apply()

    var serverUrl: String
        get() = prefs.getString("server_url", "http://192.168.1.100:8000") ?: "http://192.168.1.100:8000"
        set(value) = prefs.edit().putString("server_url", value).apply()

    init {
        loadSpeciesWhitelist()
        refreshStats()
    }

    private fun loadSpeciesWhitelist() {
        viewModelScope.launch {
            try {
                val jsonStr = getApplication<Application>().assets.open("ru_birds_whitelist.json")
                    .bufferedReader().use { it.readText() }
                val obj = JSONObject(jsonStr)
                val list = mutableListOf<SpeciesInfo>()
                for (key in obj.keys()) {
                    val item = obj.getJSONObject(key)
                    list.add(
                        SpeciesInfo(
                            slug = key,
                            ruName = item.optString("ru", key),
                            habitat = item.optString("habitat", "unknown")
                        )
                    )
                }
                _speciesList.value = list.sortedBy { it.ruName }
            } catch (ignored: Exception) {}
        }
    }

    fun startListening() {
        ContinuousListeningService.startService(getApplication())
    }

    fun pauseListening() {
        ContinuousListeningService.pauseService(getApplication())
    }

    fun resumeListening() {
        ContinuousListeningService.resumeService(getApplication())
    }

    fun stopListening() {
        ContinuousListeningService.stopService(getApplication())
    }

    fun refreshStats() {
        viewModelScope.launch {
            _statsSummary.value = statsAggregator.getSummary()
        }
    }

    fun correctSpecies(detectionId: Long, newSpeciesRu: String) {
        viewModelScope.launch {
            val detection = dao.getById(detectionId) ?: return@launch
            val updated = detection.copy(userCorrectedSpecies = newSpeciesRu)
            dao.update(updated)
            refreshStats()
        }
    }

    fun exportCsv(onReady: (File) -> Unit) {
        viewModelScope.launch {
            val list = dao.getAllList()
            val csv = statsAggregator.exportToCsv(list)
            val file = statsAggregator.saveExportFile(
                getApplication(),
                "detections_${System.currentTimeMillis()}.csv",
                csv
            )
            onReady(file)
        }
    }

    fun exportGeoJson(onReady: (File) -> Unit) {
        viewModelScope.launch {
            val list = dao.getAllList()
            val geoJson = statsAggregator.exportToGeoJson(list)
            val file = statsAggregator.saveExportFile(
                getApplication(),
                "detections_${System.currentTimeMillis()}.geojson",
                geoJson
            )
            onReady(file)
        }
    }

    fun syncWithServer(onResult: (SyncResult) -> Unit) {
        if (!isNetworkSyncEnabled) {
            onResult(SyncResult(false, 0, "Синхронизация отключена в настройках"))
            return
        }
        viewModelScope.launch {
            val deviceId = prefs.getString("device_id", null) ?: run {
                val newId = "android_" + java.util.UUID.randomUUID().toString().take(8)
                prefs.edit().putString("device_id", newId).apply()
                newId
            }
            val res = syncManager.syncPendingDetections(serverUrl, deviceId)
            onResult(res)
        }
    }

    fun checkModelUpdate(manifestUrl: String, onResult: (UpdateResult) -> Unit) {
        viewModelScope.launch {
            val res = updateManager.checkAndUpdateModel(manifestUrl)
            onResult(res)
        }
    }
}
