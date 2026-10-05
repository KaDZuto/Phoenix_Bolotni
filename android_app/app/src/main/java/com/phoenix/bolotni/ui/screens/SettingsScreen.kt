package com.phoenix.bolotni.ui.screens

import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.phoenix.bolotni.model.UpdateResult
import com.phoenix.bolotni.network.SyncResult
import com.phoenix.bolotni.ui.MainViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(viewModel: MainViewModel) {
    val context = LocalContext.current
    var isSyncEnabled by remember { mutableStateOf(viewModel.isNetworkSyncEnabled) }
    var serverUrl by remember { mutableStateOf(viewModel.serverUrl) }
    var manifestUrl by remember { mutableStateOf("https://raw.githubusercontent.com/KaDZuto/Phoenix_Bolotni/main/models/manifest.json") }
    var isCheckingUpdate by remember { mutableStateOf(false) }
    var isSyncing by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Настройки и приватность", fontWeight = FontWeight.Bold) }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // Карточка приватности (Задача 3 [P1])
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(14.dp),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFE8F5E9))
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Security, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text("Приватность и обработка данных", fontWeight = FontWeight.Bold)
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "• Всё распознавание выполняется 100% локально на вашем смартфоне.\n" +
                                "• Полные многочасовые аудиозаписи никогда не сохраняются на диск.\n" +
                                "• Сохраняются только 5-секундные подтверждённые фрагменты для личного прослушивания.\n" +
                                "• Без вашего прямого согласия никакие данные не отправляются наружу.",
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            }

            // Настройка участия в сети мониторинга (Задача 4 [P2])
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(14.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Участвовать в сети наблюдений",
                                fontWeight = FontWeight.Bold
                            )
                            Text(
                                text = "По умолчанию выключено. Отправляет на сервер ТОЛЬКО обезличенную статистику видов (не аудио) при наличии интернета.",
                                style = MaterialTheme.typography.bodySmall,
                                color = Color.Gray
                            )
                        }
                        Switch(
                            checked = isSyncEnabled,
                            onCheckedChange = {
                                isSyncEnabled = it
                                viewModel.isNetworkSyncEnabled = it
                            }
                        )
                    }

                    if (isSyncEnabled) {
                        Spacer(modifier = Modifier.height(12.dp))
                        OutlinedTextField(
                            value = serverUrl,
                            onValueChange = {
                                serverUrl = it
                                viewModel.serverUrl = it
                            },
                            label = { Text("Адрес сервера (POST /ingest)") },
                            modifier = Modifier.fillMaxWidth()
                        )

                        Spacer(modifier = Modifier.height(8.dp))
                        Button(
                            onClick = {
                                isSyncing = true
                                viewModel.syncWithServer { res ->
                                    isSyncing = false
                                    val msg = if (res.success) "Синхронизировано: ${res.syncedCount} детекций" else "Ошибка: ${res.errorMessage}"
                                    Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
                                }
                            },
                            enabled = !isSyncing,
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text(if (isSyncing) "Синхронизация…" else "Синхронизировать сейчас")
                        }
                    }
                }
            }

            // Обновление модели без магазина приложений (Задача 6 [P2])
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(14.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "Обновление модели классификации",
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "Позволяет обновлять веса ONNX и списки видов без переустановки APK. Проверяет SHA-256 с авто-откатом.",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color.Gray
                    )

                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = manifestUrl,
                        onValueChange = { manifestUrl = it },
                        label = { Text("URL манифеста модели") },
                        modifier = Modifier.fillMaxWidth()
                    )

                    Spacer(modifier = Modifier.height(12.dp))
                    OutlinedButton(
                        onClick = {
                            isCheckingUpdate = true
                            viewModel.checkModelUpdate(manifestUrl) { res ->
                                isCheckingUpdate = false
                                val msg = when (res) {
                                    is UpdateResult.Success -> "Модель обновлена до версии ${res.version}!"
                                    is UpdateResult.UpToDate -> "Установлена самая свежая версия модели"
                                    is UpdateResult.Error -> "Ошибка обновления: ${res.message}"
                                }
                                Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
                            }
                        },
                        enabled = !isCheckingUpdate,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Icon(Icons.Default.CloudDownload, contentDescription = null)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(if (isCheckingUpdate) "Проверка…" else "Проверить обновление модели")
                    }
                }
            }
        }
    }
}
