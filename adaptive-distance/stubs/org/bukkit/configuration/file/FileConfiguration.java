package org.bukkit.configuration.file;
public abstract class FileConfiguration {
    public int getInt(String path, int def) { return def; }
    public double getDouble(String path, double def) { return def; }
}
