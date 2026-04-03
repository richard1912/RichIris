import 'package:flutter/material.dart';

class DetectionColors {
  static const person = Color(0xFFF59E0B);    // amber
  static const vehicle = Color(0xFF818CF8);   // indigo
  static const animal = Color(0xFF34D399);    // emerald
  static const motionOnly = Color(0xFF6B7280); // gray

  static const _vehicleLabels = {'bicycle', 'car', 'motorcycle', 'bus', 'truck'};

  static Color forLabel(String? label) {
    if (label == null) return motionOnly;
    if (label == 'person') return person;
    if (_vehicleLabels.contains(label)) return vehicle;
    return animal;
  }
}
