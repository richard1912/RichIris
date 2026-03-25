import 'package:flutter_test/flutter_test.dart';
import 'package:richiris/app.dart';

void main() {
  testWidgets('App launches', (WidgetTester tester) async {
    await tester.pumpWidget(const RichIrisApp());
    expect(find.byType(RichIrisApp), findsOneWidget);
  });
}
