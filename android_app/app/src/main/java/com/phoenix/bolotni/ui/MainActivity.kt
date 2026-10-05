package com.phoenix.bolotni.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import com.phoenix.bolotni.local.DetectionEntity
import com.phoenix.bolotni.service.ServiceListeningState
import com.phoenix.bolotni.ui.screens.*
import com.phoenix.bolotni.ui.theme.PhoenixBolotniTheme

enum class Screen {
    HOME,
    HISTORY,
    GUIDE,
    SETTINGS,
    DETAIL
}

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    private val requestPermissionsLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        // Права получены
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        checkAndRequestPermissions()

        setContent {
            PhoenixBolotniTheme {
                MainAppContainer(viewModel)
            }
        }
    }

    private fun checkAndRequestPermissions() {
        val permissionsToRequest = mutableListOf(
            Manifest.permission.RECORD_AUDIO
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissionsToRequest.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        permissionsToRequest.add(Manifest.permission.ACCESS_FINE_LOCATION)

        val missing = permissionsToRequest.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }

        if (missing.isNotEmpty()) {
            requestPermissionsLauncher.launch(missing.toTypedArray())
        }
    }
}

@Composable
fun MainAppContainer(viewModel: MainViewModel) {
    var currentScreen by remember { mutableStateOf(Screen.HOME) }
    var selectedDetection by remember { mutableStateOf<DetectionEntity?>(null) }

    val listeningState by viewModel.listeningState.collectAsState()
    val detections by viewModel.detections.collectAsState()
    val speciesList by viewModel.speciesList.collectAsState()
    val statsSummary by viewModel.statsSummary.collectAsState()

    Scaffold(
        bottomBar = {
            if (currentScreen != Screen.DETAIL) {
                NavigationBar {
                    NavigationBarItem(
                        selected = currentScreen == Screen.HOME,
                        onClick = { currentScreen = Screen.HOME },
                        icon = { Icon(Icons.Default.Hearing, contentDescription = null) },
                        label = { Text("Главная") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == Screen.HISTORY,
                        onClick = {
                            viewModel.refreshStats()
                            currentScreen = Screen.HISTORY
                        },
                        icon = { Icon(Icons.Default.MenuBook, contentDescription = null) },
                        label = { Text("Дневник") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == Screen.GUIDE,
                        onClick = { currentScreen = Screen.GUIDE },
                        icon = { Icon(Icons.Default.NaturePeople, contentDescription = null) },
                        label = { Text("Птицы") }
                    )
                    NavigationBarItem(
                        selected = currentScreen == Screen.SETTINGS,
                        onClick = { currentScreen = Screen.SETTINGS },
                        icon = { Icon(Icons.Default.Settings, contentDescription = null) },
                        label = { Text("Настройки") }
                    )
                }
            }
        }
    ) { padding ->
        Box(modifier = Modifier.padding(padding)) {
            when (currentScreen) {
                Screen.HOME -> HomeScreen(
                    listeningState = listeningState,
                    detections = detections,
                    onToggleListening = {
                        if (listeningState == ServiceListeningState.LISTENING) {
                            viewModel.pauseListening()
                        } else if (listeningState == ServiceListeningState.PAUSED) {
                            viewModel.resumeListening()
                        } else {
                            viewModel.startListening()
                        }
                    },
                    onDetectionClick = { item ->
                        selectedDetection = item
                        currentScreen = Screen.DETAIL
                    }
                )
                Screen.HISTORY -> HistoryScreen(
                    detections = detections,
                    statsSummary = statsSummary,
                    onExportCsv = { onReady -> viewModel.exportCsv(onReady) },
                    onExportGeoJson = { onReady -> viewModel.exportGeoJson(onReady) },
                    onDetectionClick = { item ->
                        selectedDetection = item
                        currentScreen = Screen.DETAIL
                    }
                )
                Screen.GUIDE -> SpeciesGuideScreen(speciesList = speciesList)
                Screen.SETTINGS -> SettingsScreen(viewModel = viewModel)
                Screen.DETAIL -> {
                    val detection = selectedDetection
                    if (detection != null) {
                        DetectionDetailScreen(
                            detection = detection,
                            availableSpecies = speciesList,
                            onBack = { currentScreen = Screen.HOME },
                            onCorrectSpecies = { id, newSpecies ->
                                viewModel.correctSpecies(id, newSpecies)
                                selectedDetection = detection.copy(userCorrectedSpecies = newSpecies)
                            }
                        )
                    } else {
                        currentScreen = Screen.HOME
                    }
                }
            }
        }
    }
}
