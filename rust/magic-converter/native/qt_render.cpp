#include <cmath>
#include <QDir>
#include <QFile>
#include <QFont>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QImage>
#include <QAbstractTextDocumentLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QTextBlock>
#include <QMarginsF>
#include <QPageLayout>
#include <QPageSize>
#include <QPainter>
#include <QPdfWriter>
#include <QRectF>
#include <QStringList>
#include <QSvgRenderer>
#include <QTextDocument>
#include <QTextLayout>
#include <QTextFragment>
#include <QTextOption>
#include <QUrl>

class LocalDocument : public QTextDocument {
protected:
    QVariant loadResource(int type, const QUrl& url) override {
        if (url.scheme() != "data") return {};
        return QTextDocument::loadResource(type, url);
    }
};

static int html_pdf(const QStringList& args) {
    if (args.size() != 5) return 2;
    QFile source(args[2]);
    if (!source.open(QIODevice::ReadOnly) || source.size() > 128 * 1024 * 1024) return 3;
    QPdfWriter output(args[3]);
    output.setResolution(300);
    output.setPageSize(QPageSize(QPageSize::A4));
    if (args[4] == "landscape") output.setPageOrientation(QPageLayout::Landscape);
    output.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout::Millimeter);
    LocalDocument doc;
#ifdef Q_OS_MAC
    doc.setDefaultStyleSheet("body {font-family:'Noto Sans','Droid Sans Fallback'; font-size:10pt;} table {border-collapse:collapse;} td,th {border:1px solid #888;padding:4px;} p {margin:6px 0;}");
#else
    doc.setDefaultStyleSheet("body {font-family:'Malgun Gothic'; font-size:10pt;} table {border-collapse:collapse;} td,th {border:1px solid #888;padding:4px;} p {margin:6px 0;}");
#endif
    doc.setHtml(QString::fromUtf8(source.readAll()));
    doc.print(&output);
    return 0;
}

static int textbox(const QStringList& args) {
    const bool layout = args[1] == "textbox-layout";
    if (layout ? args.size() != 8 : args.size() < 11 || args.size() > 12) return 2;
    double values[2];
    for (int i = 0; i < 2; ++i) {
        bool valid;
        values[i] = args[4 + i].toDouble(&valid);
        if (!valid || !std::isfinite(values[i]) || values[i] < 0.1 || values[i] > 2000)
            return 2;
    }
    QFile source(args[2]);
    if (!source.open(QIODevice::ReadOnly) || source.size() > 128 * 1024) return 3;
    constexpr double pixelsPerPoint = 96.0 / 72.0;
    const double width = values[0] * pixelsPerPoint, height = values[1] * pixelsPerPoint;
    LocalDocument doc;
    doc.setDocumentMargin(0);
    doc.setUseDesignMetrics(true);
    auto options = doc.defaultTextOption();
    const bool fitRun = args.size() == 12 && args[11] == "fit";
    options.setWrapMode(fitRun || args[7] == "none" ? QTextOption::NoWrap : QTextOption::WrapAtWordBoundaryOrAnywhere);
    doc.setDefaultTextOption(options);
    doc.setDefaultStyleSheet("body { font-family:'Malgun Gothic'; font-size:10pt; } p { margin:0; }");
    doc.setHtml(QString::fromUtf8(source.readAll()));
    doc.setTextWidth(width);
    const double freeHeight = std::max(0.0, height - doc.size().height());
    const double y = args[6] == "CENTER" ? freeHeight / 2 : args[6] == "BOTTOM" ? freeHeight : 0;
    if (layout) {
        QRectF ink;
        QJsonArray lines;
        QJsonArray runs;
        for (auto block = doc.begin(); block.isValid(); block = block.next()) {
            const auto bounds = doc.documentLayout()->blockBoundingRect(block);
            for (int i = 0; i < block.layout()->lineCount(); ++i) {
                const auto line = block.layout()->lineAt(i);
                const auto rect = line.naturalTextRect().translated(bounds.topLeft()).translated(0, y);
                ink |= rect;
                lines.append(block.text().mid(line.textStart(), line.textLength()).trimmed());
                const auto start = line.textStart(), end = start + line.textLength();
                for (auto it = block.begin(); !it.atEnd(); ++it) {
                    const auto fragment = it.fragment();
                    const auto from = std::max(start, fragment.position() - block.position());
                    const auto to = std::min(end, fragment.position() - block.position() + fragment.length());
                    if (from >= to) continue;
                    auto text = block.text().mid(from, to - from);
                    if (text.trimmed().isEmpty()) continue;
                    const auto font = fragment.charFormat().font();
                    const auto brush = fragment.charFormat().foreground();
                    const auto color = brush.style() == Qt::NoBrush ? QColor(Qt::black) : brush.color();
                    const double a = line.cursorToX(from), b = line.cursorToX(to);
                    const double x = bounds.x() + std::min(a, b);
                    const QFontMetricsF metrics(font);
                    const double top = bounds.y() + y + line.y() + line.ascent() - metrics.ascent();
                    runs.append(QJsonArray{text, font.family(), color.name(), font.bold(), font.italic(),
                                           x/pixelsPerPoint, top/pixelsPerPoint, std::abs(b-a)/pixelsPerPoint,
                                           metrics.height()/pixelsPerPoint, font.pointSizeF()});
                    if (runs.size() > 4096) return 4;
                }
            }
        }
        QJsonArray result{ink.x()/pixelsPerPoint, ink.y()/pixelsPerPoint,
                          ink.width()/pixelsPerPoint, ink.height()/pixelsPerPoint, lines, runs};
        QFile out(args[3]);
        if (!out.open(QIODevice::WriteOnly)) return 5;
        const auto encoded = QJsonDocument(result).toJson(QJsonDocument::Compact);
        return out.write(encoded) == encoded.size() ? 0 : 5;
    }
    bool valid;
    const double angle = args[8].toDouble(&valid);
    if (!valid || !std::isfinite(angle)) return 2;
    const auto radians = angle * std::acos(-1.0) / 180;
    const double w = std::abs(std::cos(radians)) * width + std::abs(std::sin(radians)) * height;
    const double h = std::abs(std::sin(radians)) * width + std::abs(std::cos(radians)) * height;
    auto draw = [&](QPaintDevice& target) {
        QPainter painter(&target);
        if (!painter.isActive()) return false;
        painter.setRenderHint(QPainter::TextAntialiasing);
        painter.scale(target.width() / w, target.height() / h);
        painter.translate(w / 2, h / 2);
        painter.rotate(angle);
        painter.scale(args[9] == "1" ? -1 : 1, args[10] == "1" ? -1 : 1);
        painter.translate(-width / 2, -height / 2);
        painter.setClipRect(QRectF(0, 0, width, height));
        if (fitRun) {
            doc.adjustSize();
            const auto extent = doc.size();
            painter.scale(width / std::max(1.0, extent.width()), height / std::max(1.0, extent.height()));
        } else {
            painter.translate(0, y);
        }
        doc.drawContents(&painter);
        return painter.end();
    };
    if (args[1] == "textbox-png") {
        const auto iw = int(std::ceil(w * 3)), ih = int(std::ceil(h * 3));
        if (qint64(iw) * ih > 24000000) return 4;
        QImage image(iw, ih, QImage::Format_ARGB32_Premultiplied);
        if (image.isNull()) return 4;
        image.fill(0);
        return draw(image) && image.save(args[3], "PNG") ? 0 : 5;
    }
    QPdfWriter output(args[3]);
    output.setResolution(720);
    if (!output.setPageSize(QPageSize(QSizeF(w/pixelsPerPoint, h/pixelsPerPoint), QPageSize::Point,
                                     QString(), QPageSize::ExactMatch))
        || !output.setPageMargins(QMarginsF(), QPageLayout::Point)) return 5;
    return draw(output) ? 0 : 5;
}

static int svg_png(QSvgRenderer& renderer, const QString& output) {
    const QSize size = renderer.defaultSize();
    if (size.isEmpty() || qint64(size.width()) * size.height() > 80000000) return 4;
    QImage image(size, QImage::Format_ARGB32_Premultiplied);
    if (image.isNull()) return 4;
    image.fill(0);
    QPainter painter(&image);
    renderer.render(&painter);
    painter.end();
    return image.save(output, "PNG") ? 0 : 5;
}

static int svg_pdf(QSvgRenderer& renderer, const QStringList& args) {
    if (args.size() != 14) return 2;
    double values[10];
    for (int i = 0; i < 10; ++i) {
        bool valid;
        values[i] = args[i + 4].toDouble(&valid);
        if (!valid || !std::isfinite(values[i])) return 2;
    }
    const double width = values[0], height = values[1];
    if (width <= 0 || height <= 0 || width * height > 80000000) return 4;
    renderer.setViewBox(QRectF(values[2], values[3], values[4], values[5]));
    renderer.setAspectRatioMode(Qt::IgnoreAspectRatio);
    QPdfWriter output(args[3]);
    output.setResolution(720);
    const QPageSize size(QSizeF(width * 0.75, height * 0.75), QPageSize::Point, QString(), QPageSize::ExactMatch);
    if (!output.setPageSize(size) || !output.setPageMargins(QMarginsF(), QPageLayout::Point)) return 5;
    QPainter painter(&output);
    if (!painter.isActive()) return 5;
    painter.scale(output.width() / width, output.height() / height);
    painter.setClipRect(QRectF(0, 0, width, height));
    renderer.render(&painter, QRectF(values[6], values[7], values[8], values[9]));
    painter.end();
    return 0;
}

int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    if (!qEnvironmentVariableIsSet("QT_QPA_FONTDIR")) {
        const QString fonts = QDir(qEnvironmentVariable("WINDIR", "C:/Windows")).filePath("Fonts");
        if (QDir(fonts).exists()) qputenv("QT_QPA_FONTDIR", fonts.toLocal8Bit());
    }
    QGuiApplication app(argc, argv);
#ifdef Q_OS_MAC
    const QDir fonts(QCoreApplication::applicationDirPath() + "/fonts");
    for (const QString& file : fonts.entryList({"*.otf", "*.ttf"}, QDir::Files)) {
        if (QFontDatabase::addApplicationFont(fonts.filePath(file)) < 0) return 4;
    }
    for (const QString& family : {"Malgun Gothic", "Gulim", "Dotum", "Segoe UI"})
        QFont::insertSubstitutions(family, {"Noto Sans", "Droid Sans Fallback"});
    for (const auto script : {QChar::Script_Hangul, QChar::Script_Han, QChar::Script_Hiragana, QChar::Script_Katakana})
        QFontDatabase::addApplicationFallbackFontFamily(script, "Droid Sans Fallback");
#endif
    const QStringList args = QCoreApplication::arguments();
    if (args.size() < 4) return 2;
    if (args[1] == "html-pdf") return html_pdf(args);
    if (args[1] == "textbox-layout" || args[1] == "textbox-pdf" || args[1] == "textbox-png")
        return textbox(args);
    QFile source(args[2]);
    if (!source.open(QIODevice::ReadOnly) || source.size() > 32 * 1024 * 1024) return 3;
    QSvgRenderer renderer(source.readAll());
    if (!renderer.isValid()) return 3;
    if (args[1] == "svg-png" && args.size() == 4) return svg_png(renderer, args[3]);
    if (args[1] == "svg-pdf") return svg_pdf(renderer, args);
    return 2;
}
