package com.phoenix.bolotni.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
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
import com.phoenix.bolotni.local.DetectionEntity
import com.phoenix.bolotni.local.StatsSummary
import com.phoenix.bolotni.network.ShareIntentHelper
import java.io.File

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HistoryScreen(
    detections: List<DetectionEntity>,
    statsSummary: StatsSummary?,
    onExportCsv: ((File) -> Unit) -> Unit,
    onExportGeoJson: ((File) -> Unit) -> Unit,
    onDetectionClick: (DetectionEntity) -> Unit
) {
    val context = LocalContext.current
    var selectedHabitatFilter by remember { mutableStateOf<String?>(null) }
    var searchQuery by remember { mutableStateOf("") }

    val habitats = listOf("all", "wetland", "forest", "field_steppe", "urban")

    val filteredList = detections.filter { item ->
        val habitatMatch = (selectedHabitatFilter == null || selectedHabitatFilter == "all" || item.habitat == selectedHabitatFilter)
        val searchMatch = searchQuery.isBlank() ||
                item.speciesRu.contains(searchQuery, ignoreCase = true) ||
                item.speciesSlug.contains(searchQuery, ignoreCase = true)
        habitatMatch && searchMatch
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Дневник наблюдений", fontWeight = FontWeight.Bold) }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
        ) {
            // Аналитическая сводка (Задача 7 [P0])
            if (statsSummary != null) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(bottom = 12.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
                ) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text(
                            text = "Офлайн-статистика местности",
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onPrimaryContainer
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text("Всего находок: ${statsSummary.totalDetections}")
                            Text("Уникальных видов: ${statsSummary.uniqueSpeciesCount}")
                        }
                    }
                }
            }

            // Поиск
            OutlinedTextField(
                value = searchQuery,
                onValueChange = { searchQuery = it },
                label = { Text("Поиск по виду") },
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = null) },
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(modifier = Modifier.height(8.dp))

            // Фильтры по биотопу
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                habitats.forEach { h ->
                    val label = when (h) {
                        "all" -> "Все"
                        "wetland" -> "Болота"
                        "forest" -> "Леса"
                        "field_steppe" -> "Степи"
                        "urban" -> "Город"
                        else -> h
                    }
                    FilterChip(
                        selected = (selectedHabitatFilter == h || (h == "all" && selectedHabitatFilter == null)),
                        onClick = { selectedHabitatFilter = if (h == "all") null else h },
                        label = { Text(label) }
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // Кнопки офлайн-экспорта (Задача 7 [P0])
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                OutlinedButton(
                    onClick = {
                        onExportCsv { file ->
                            val intent = ShareIntentHelper.shareExportedFile(context, file, "text/csv")
                            context.startActivity(intent)
                        }
                    },
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.Download, contentDescription = null)
                    Spacer(modifier = Modifier.width(4.dp))
                    Text("Экспорт CSV")
                }

                OutlinedButton(
                    onClick = {
                        onExportGeoJson { file ->
                            val intent = ShareIntentHelper.shareExportedFile(context, file, "application/geo+json")
                            context.startActivity(intent)
                        }
                    },
                    modifier = Modifier.weight(1f)
                ) {
                    Icon(Icons.Default.Map, contentDescription = null)
                    Spacer(modifier = Modifier.width(4.dp))
                    Text("Экспорт GeoJSON")
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            if (filteredList.isEmpty()) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .weight(1f),
                    contentAlignment = Alignment.Center
                ) {
                    Text("Нет наблюдений по выбранным фильтрам", color = Color.Gray)
                }
            } else {
                LazyColumn(
                    modifier = Modifier.weight(1f),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    items(filteredList, key = { it.id }) { item ->
                        DetectionCard(
                            detection = item,
                            onClick = { onDetectionClick(item) },
                            onShare = {
                                val shareIntent = ShareIntentHelper.shareSnippetForExpertReview(context, item)
                                if (shareIntent != null) context.startActivity(shareIntent)
                            }
                        )
                    }
                }
            }
        }
    }
}
