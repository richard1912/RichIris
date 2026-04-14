import 'package:flutter/material.dart';

enum DetectionCategory { person, vehicle, animal, motionOnly }

class DetectionColors {
  static const person = Color(0xFFF59E0B);    // amber
  static const vehicle = Color(0xFF818CF8);   // indigo
  static const animal = Color(0xFF34D399);    // emerald
  static const motionOnly = Color(0xFF6B7280); // gray
  static const faceKnown = Color(0xFF06B6D4);   // cyan — known face recognized
  static const faceUnknown = Color(0xFFE11D48); // rose — unknown face seen

  static const _vehicleLabels = {'bicycle', 'car', 'motorcycle', 'bus', 'truck'};

  static Color forLabel(String? label) {
    if (label == null) return motionOnly;
    if (label == 'person') return person;
    if (_vehicleLabels.contains(label)) return vehicle;
    return animal;
  }

  static DetectionCategory categoryFor(String? label) {
    if (label == null) return DetectionCategory.motionOnly;
    if (label == 'person') return DetectionCategory.person;
    if (_vehicleLabels.contains(label)) return DetectionCategory.vehicle;
    return DetectionCategory.animal;
  }

  static Color forCategory(DetectionCategory cat) {
    switch (cat) {
      case DetectionCategory.person: return person;
      case DetectionCategory.vehicle: return vehicle;
      case DetectionCategory.animal: return animal;
      case DetectionCategory.motionOnly: return motionOnly;
    }
  }

  static String labelFor(DetectionCategory cat) {
    switch (cat) {
      case DetectionCategory.person: return 'Person';
      case DetectionCategory.vehicle: return 'Vehicle';
      case DetectionCategory.animal: return 'Animal';
      case DetectionCategory.motionOnly: return 'Motion';
    }
  }
}
