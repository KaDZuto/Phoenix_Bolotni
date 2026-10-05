package com.phoenix.bolotni.service

import android.annotation.SuppressLint
import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationManager
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.phoenix.bolotni.R
import com.phoenix.bolotni.local.DetectionDao
import com.phoenix.bolotni.local.DetectionEntity
import com.phoenix.bolotni.local.DetectionsDatabase
import com.phoenix.bolotni.model.*
import com.phoenix.bolotni.ui.MainActivity
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow

enum class ServiceListeningState {
    STOPPED,
    LISTENING,
    PAUSED
}

/**
 * Android Foreground Service для непрерывного фонового распознавания птиц (Задача 2 и 3).
 * Гарантирует стабильную работу без выгрузки системой по политике энергосбережения Android.
 */
class ContinuousListeningService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.Default + SupervisorJob())

    private lateinit var database: DetectionsDatabase
    private lateinit var dao: DetectionDao
    private lateinit var modelRunner: ModelRunner
    private lateinit var voteEngine: WindowVoteEngine
    private lateinit var snippetRecorder: AudioSnippetRecorder
    private var captureManager: AudioCaptureManager? = null

    private var lastLocation: Location? = null

    companion object {
        const val ACTION_START = "com.phoenix.bolotni.action.START"
        const val ACTION_PAUSE = "com.phoenix.bolotni.action.PAUSE"
        const val ACTION_RESUME = "com.phoenix.bolotni.action.RESUME"
        const val ACTION_STOP = "com.phoenix.bolotni.action.STOP"

        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "phoenix_bolotni_listening"

        private val _listeningState = MutableStateFlow(ServiceListeningState.STOPPED)
        val listeningState = _listeningState.asStateFlow()

        private val _candidateEvents = MutableSharedFlow<CandidateDetection>(extraBufferCapacity = 64)
        val candidateEvents = _candidateEvents.asSharedFlow()

        private val _confirmedEvents = MutableSharedFlow<ConfirmedDetection>(extraBufferCapacity = 64)
        val confirmedEvents = _confirmedEvents.asSharedFlow()

        fun startService(context: Context) {
            val intent = Intent(context, ContinuousListeningService::class.java).apply {
                action = ACTION_START
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun pauseService(context: Context) {
            val intent = Intent(context, ContinuousListeningService::class.java).apply {
                action = ACTION_PAUSE
            }
            context.startService(intent)
        }

        fun resumeService(context: Context) {
            val intent = Intent(context, ContinuousListeningService::class.java).apply {
                action = ACTION_RESUME
            }
            context.startService(intent)
        }

        fun stopService(context: Context) {
            val intent = Intent(context, ContinuousListeningService::class.java).apply {
                action = ACTION_STOP
            }
            context.startService(intent)
        }
    }

    override fun onCreate() {
        super.onCreate()
        database = DetectionsDatabase.getInstance(this)
        dao = database.detectionDao()
        snippetRecorder = AudioSnippetRecorder(this)

        modelRunner = ModelRunner()
        try {
            modelRunner.initializeFromAssets(this)
        } catch (e: Exception) {
            // Если модель обновлена из внешнего хранилища — раннер подхватит её
        }

        voteEngine = WindowVoteEngine(modelRunner.config) { confirmed ->
            serviceScope.launch {
                _confirmedEvents.emit(confirmed)
                updateNotification("Услышано: ${confirmed.speciesRu}")
            }
        }

        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startListening()
            ACTION_PAUSE -> pauseListening()
            ACTION_RESUME -> resumeListening()
            ACTION_STOP -> stopListening()
            else -> startListening()
        }
        return START_STICKY
    }

    private fun startListening() {
        val notification = buildNotification(getString(R.string.listening_active))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        _listeningState.value = ServiceListeningState.LISTENING
        voteEngine.reset()
        updateCurrentLocation()

        captureManager = AudioCaptureManager(
            config = modelRunner.config,
            preprocessor = modelRunner.preprocessor
        ) { windowIndex, audioWindow ->
            handleAudioWindow(windowIndex, audioWindow)
        }
        captureManager?.start(serviceScope)
    }

    private fun pauseListening() {
        captureManager?.stop()
        captureManager = null
        _listeningState.value = ServiceListeningState.PAUSED
        updateNotification(getString(R.string.listening_paused))
    }

    private fun resumeListening() {
        if (_listeningState.value == ServiceListeningState.PAUSED) {
            startListening()
        }
    }

    private fun stopListening() {
        captureManager?.stop()
        captureManager = null
        _listeningState.value = ServiceListeningState.STOPPED
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun handleAudioWindow(windowIndex: Long, audioWindow: FloatArray) {
        val now = System.currentTimeMillis()
        if (!modelRunner.isInitialized) return

        try {
            val windowResult = modelRunner.predictWindow(audioWindow)
            val (candidate, confirmed) = voteEngine.processWindow(windowIndex, now, windowResult)

            serviceScope.launch {
                _candidateEvents.emit(candidate)

                if (confirmed != null) {
                    // Сохраняем локальный 5-секундный аудио-сниппет
                    val snippetFile = snippetRecorder.saveWavSnippet(
                        audio = audioWindow,
                        sampleRate = modelRunner.config.targetSr,
                        prefix = confirmed.speciesSlug
                    )

                    // Запись в локальную базу данных Room (Задача 7 [P0])
                    val entity = DetectionEntity(
                        speciesSlug = confirmed.speciesSlug,
                        speciesRu = confirmed.speciesRu,
                        habitat = confirmed.habitat,
                        confidence = confirmed.averageConfidence,
                        votesCount = confirmed.votesCount,
                        windowStartTs = now - (modelRunner.config.clipSeconds * 1000).toLong(),
                        windowEndTs = now,
                        lat = lastLocation?.latitude,
                        lon = lastLocation?.longitude,
                        createdAt = now,
                        audioSnippetPath = snippetFile.absolutePath,
                        syncStatus = 0
                    )
                    dao.insert(entity)
                }
            }
        } catch (ignored: Exception) {}
    }

    @SuppressLint("MissingPermission")
    private fun updateCurrentLocation() {
        try {
            val lm = getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            val provider = lm?.getBestProvider(android.location.Criteria(), true)
            if (provider != null) {
                lastLocation = lm.getLastKnownLocation(provider)
            }
        } catch (ignored: Exception) {}
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.channel_desc)
                setShowBadge(false)
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(statusText: String): Notification {
        val openIntent = Intent(this, MainActivity::class.java)
        val pendingOpen = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val isPaused = _listeningState.value == ServiceListeningState.PAUSED
        val toggleActionIntent = Intent(this, ContinuousListeningService::class.java).apply {
            action = if (isPaused) ACTION_RESUME else ACTION_PAUSE
        }
        val pendingToggle = PendingIntent.getService(
            this, 1, toggleActionIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = Intent(this, ContinuousListeningService::class.java).apply {
            action = ACTION_STOP
        }
        val pendingStop = PendingIntent.getService(
            this, 2, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(statusText)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentIntent(pendingOpen)
            .setOngoing(true)
            .addAction(
                if (isPaused) android.R.drawable.ic_media_play else android.R.drawable.ic_media_pause,
                if (isPaused) getString(R.string.action_resume) else getString(R.string.action_pause),
                pendingToggle
            )
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, getString(R.string.action_stop), pendingStop)
            .build()
    }

    private fun updateNotification(statusText: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(statusText))
    }

    override fun onDestroy() {
        stopListening()
        serviceScope.cancel()
        modelRunner.close()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
