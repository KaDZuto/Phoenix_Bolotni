package com.phoenix.bolotni.ui.screens

import android.media.MediaPlayer
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.phoenix.bolotni.local.DetectionEntity
import com.phoenix.bolotni.model.SpeciesInfo
import com.phoenix.bolotni.network.ShareIntentHelper
import com.phoenix.bolotni.ui.theme.StatusListening
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DetectionDetailScreen(
    detection: DetectionEntity,
    availableSpecies: List<SpeciesInfo>,
    onBack: () -> Unit,
    onCorrectSpecies: (Long, String) -> Unit
) {
    val context = LocalContext.current
    var isPlaying by remember { mutableStateOf(false) }
    var mediaPlayer by remember { mutableStateOf<MediaPlayer?>(null) }
    var showCorrectionDialog by remember { mutableStateOf(false) }

    val dateFormat = remember { SimpleDateFormat("dd MMMM yyyy, HH:mm:ss", Locale("ru")) }
    val timeFormatted = dateFormat.format(Date(detection.createdAt))

    DisposableEffect(Unit) {
        onDispose {
            mediaPlayer?.release()
            mediaPlayer = null
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Детали наблюдения") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, contentDescription = "Назад")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
        ) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = detection.userCorrectedSpecies ?: detection.speciesRu,
                        style = MaterialTheme.typography.headlineSmall,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "Латынь: ${detection.speciesSlug}",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color.Gray
                    )

                    Spacer(modifier = Modifier.height(8.dp))
                    HabitatBadge(habitat = detection.habitat)

                    if (detection.userCorrectedSpecies != null) {
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = "Скорректировано вручную (было: ${detection.speciesRu})",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.primary
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Карточка достоверности (Многократная проверка)
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(containerColor = Color(0xFFF1F8E9))
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Default.Verified, contentDescription = null, tint = StatusListening)
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            text = "Результат многократной проверки",
                            fontWeight = FontWeight.Bold,
                            color = StatusListening
                        )
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Подтверждено в ${detection.votesCount} из 3 последовательных окон прослушивания. " +
                                "Исключена случайная шумовая погрешность.",
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Время записи: $timeFormatted",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color.Gray
                    )
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Аудио-плеер локального фрагмента (Задача 5 [P1])
            val snippetPath = detection.audioSnippetPath
            if (snippetPath != null && File(snippetPath).exists()) {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column {
                            Text(
                                text = "Локальный аудио-фрагмент (5 сек)",
                                fontWeight = FontWeight.SemiBold
                            )
                            Text(
                                text = if (isPlaying) "Воспроизведение…" else "Нажмите для прослушивания",
                                style = MaterialTheme.typography.bodySmall,
                                color = Color.Gray
                            )
                        }

                        IconButton(
                            onClick = {
                                if (isPlaying) {
                                    mediaPlayer?.stop()
                                    mediaPlayer?.release()
                                    mediaPlayer = null
                                    isPlaying = false
                                } else {
                                    try {
                                        mediaPlayer = MediaPlayer().apply {
                                            setDataSource(snippetPath)
                                            prepare()
                                            setOnCompletionListener {
                                                isPlaying = false
                                                release()
                                                mediaPlayer = null
                                            }
                                            start()
                                        }
                                        isPlaying = true
                                    } catch (e: Exception) {
                                        isPlaying = false
                                    }
                                }
                            }
                        ) {
                            Icon(
                                imageVector = if (isPlaying) Icons.Default.Stop else Icons.Default.PlayCircle,
                                contentDescription = "Play/Stop",
                                modifier = Modifier.size(40.dp),
                                tint = MaterialTheme.colorScheme.primary
                            )
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(24.dp))

            // Кнопка ручной коррекции вида
            OutlinedButton(
                onClick = { showCorrectionDialog = true },
                modifier = Modifier.fillMaxWidth()
            ) {
                Icon(Icons.Default.Edit, contentDescription = null)
                Spacer(modifier = Modifier.width(8.dp))
                Text("Скорректировать вид в личном дневнике")
            }

            Spacer(modifier = Modifier.height(12.dp))

            // Кнопка «Отправить на экспертную разметку в Telegram» (Задача 4 [P2])
            Button(
                onClick = {
                    val shareIntent = ShareIntentHelper.shareSnippetForExpertReview(context, detection)
                    if (shareIntent != null) context.startActivity(shareIntent)
                },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary)
            ) {
                Icon(Icons.Default.Send, contentDescription = null)
                Spacer(modifier = Modifier.width(8.dp))
                Text("Отправить на экспертную разметку в Telegram")
            }
        }
    }

    if (showCorrectionDialog) {
        AlertDialog(
            onDismissRequest = { showCorrectionDialog = false },
            title = { Text("Выберите верный вид") },
            text = {
                var searchText by remember { mutableStateOf("") }
                val filtered = availableSpecies.filter {
                    it.ruName.contains(searchText, ignoreCase = true) ||
                            it.slug.contains(searchText, ignoreCase = true)
                }

                Column(modifier = Modifier.fillMaxWidth()) {
                    OutlinedTextField(
                        value = searchText,
                        onValueChange = { searchText = it },
                        label = { Text("Поиск вида") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Box(modifier = Modifier.height(200.dp)) {
                        androidx.compose.foundation.lazy.LazyColumn {
                            items(filtered.size) { i ->
                                val sp = filtered[i]
                                TextButton(
                                    onClick = {
                                        onCorrectSpecies(detection.id, sp.ruName)
                                        showCorrectionDialog = false
                                    },
                                    modifier = Modifier.fillMaxWidth()
                                ) {
                                    Text("${sp.ruName} (${sp.slug})")
                                }
                            }
                        }
                    }
                }
            },
            confirmButton = {},
            dismissButton = {
                TextButton(onClick = { showCorrectionDialog = false }) {
                    Text("Отмена")
                }
            }
        )
    }
}
