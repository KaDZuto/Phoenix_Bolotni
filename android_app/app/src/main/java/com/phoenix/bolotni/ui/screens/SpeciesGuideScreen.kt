package com.phoenix.bolotni.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.phoenix.bolotni.model.SpeciesInfo

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SpeciesGuideScreen(speciesList: List<SpeciesInfo>) {
    var searchQuery by remember { mutableStateOf("") }
    var selectedHabitat by remember { mutableStateOf<String?>(null) }

    val habitats = listOf("all", "wetland", "forest", "field_steppe", "urban")

    val filteredList = speciesList.filter { sp ->
        val habitatMatch = selectedHabitat == null || selectedHabitat == "all" || sp.habitat == selectedHabitat
        val searchMatch = searchQuery.isBlank() ||
                sp.ruName.contains(searchQuery, ignoreCase = true) ||
                sp.slug.contains(searchQuery, ignoreCase = true)
        habitatMatch && searchMatch
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Определитель птиц", fontWeight = FontWeight.Bold) }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp)
        ) {
            OutlinedTextField(
                value = searchQuery,
                onValueChange = { searchQuery = it },
                label = { Text("Поиск по названию") },
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = null) },
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(modifier = Modifier.height(8.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                habitats.forEach { h ->
                    val label = when (h) {
                        "all" -> "Все (${speciesList.size})"
                        "wetland" -> "Болота"
                        "forest" -> "Леса"
                        "field_steppe" -> "Степи"
                        "urban" -> "Город"
                        else -> h
                    }
                    FilterChip(
                        selected = (selectedHabitat == h || (h == "all" && selectedHabitat == null)),
                        onClick = { selectedHabitat = if (h == "all") null else h },
                        label = { Text(label) }
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(filteredList, key = { it.slug }) { sp ->
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(10.dp)
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(12.dp),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Column {
                                Text(
                                    text = sp.ruName,
                                    style = MaterialTheme.typography.titleMedium,
                                    fontWeight = FontWeight.Bold
                                )
                                Text(
                                    text = sp.slug,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Color.Gray
                                )
                            }
                            HabitatBadge(habitat = sp.habitat)
                        }
                    }
                }
            }
        }
    }
}
