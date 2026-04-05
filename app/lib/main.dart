import 'package:flutter/material.dart';
import 'package:media_kit/media_kit.dart';
import 'app.dart';

void main(List<String> args) {
  WidgetsFlutterBinding.ensureInitialized();
  final updateOnly = args.contains('--update-only');
  if (!updateOnly) MediaKit.ensureInitialized();
  runApp(RichIrisApp(updateOnly: updateOnly));
}
