package com.shop;

public class OrderService implements Lookup {
    public String lookup(String id) {
        return id.trim();
    }
}
