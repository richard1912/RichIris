import 'package:flutter/material.dart';

final appTheme = ThemeData(
  brightness: Brightness.dark,
  scaffoldBackgroundColor: const Color(0xFF171717), // neutral-900
  colorScheme: const ColorScheme.dark(
    primary: Color(0xFF3B82F6), // blue-500
    secondary: Color(0xFF3B82F6),
    surface: Color(0xFF262626), // neutral-800
    error: Color(0xFFEF4444), // red-500
  ),
  appBarTheme: const AppBarTheme(
    backgroundColor: Color(0xFF171717),
    foregroundColor: Colors.white,
    elevation: 0,
  ),
  cardTheme: const CardThemeData(
    color: Color(0xFF262626),
    elevation: 0,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.all(Radius.circular(8)),
    ),
  ),
  dropdownMenuTheme: const DropdownMenuThemeData(
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Color(0xFF262626),
      border: OutlineInputBorder(
        borderSide: BorderSide(color: Color(0xFF404040)),
        borderRadius: BorderRadius.all(Radius.circular(6)),
      ),
    ),
  ),
  inputDecorationTheme: const InputDecorationTheme(
    filled: true,
    fillColor: Color(0xFF262626),
    border: OutlineInputBorder(
      borderSide: BorderSide(color: Color(0xFF404040)),
      borderRadius: BorderRadius.all(Radius.circular(6)),
    ),
    enabledBorder: OutlineInputBorder(
      borderSide: BorderSide(color: Color(0xFF404040)),
      borderRadius: BorderRadius.all(Radius.circular(6)),
    ),
    focusedBorder: OutlineInputBorder(
      borderSide: BorderSide(color: Color(0xFF3B82F6)),
      borderRadius: BorderRadius.all(Radius.circular(6)),
    ),
    contentPadding: EdgeInsets.symmetric(horizontal: 12, vertical: 10),
  ),
  elevatedButtonTheme: ElevatedButtonThemeData(
    style: ElevatedButton.styleFrom(
      backgroundColor: const Color(0xFF3B82F6),
      foregroundColor: Colors.white,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
    ),
  ),
  textButtonTheme: TextButtonThemeData(
    style: TextButton.styleFrom(
      foregroundColor: const Color(0xFF3B82F6),
    ),
  ),
);
