package org.bukkit;
public interface World {
    String getName();
    int getViewDistance();
    void setViewDistance(int d);
    int getSimulationDistance();
    void setSimulationDistance(int d);
}
